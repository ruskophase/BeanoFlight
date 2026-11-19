"""Persistent pinned-buffer TensorRT execution for the stereo ResNet18 model."""

from __future__ import annotations

import ctypes
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

DEFAULT_TENSORRT_ENGINE = (
    Path(__file__).resolve().parents[2]
    / "artifacts/mock-resnet18/model/mock-stereo-resnet18-fp16.engine"
)


class TensorRTInferenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TensorRTBatchResult:
    logits: tuple[tuple[float, ...], ...]
    preprocessing_ms: float
    execution_ms: float
    total_ms: float


class _CudaRuntime:
    HOST_TO_DEVICE = 1
    DEVICE_TO_HOST = 2

    def __init__(self) -> None:
        try:
            self.library = ctypes.CDLL("libcudart.so")
        except OSError as exc:
            raise TensorRTInferenceError(f"cannot load CUDA runtime: {exc}") from exc
        self.library.cudaGetErrorString.argtypes = [ctypes.c_int]
        self.library.cudaGetErrorString.restype = ctypes.c_char_p
        self.library.cudaMalloc.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_size_t,
        ]
        self.library.cudaFree.argtypes = [ctypes.c_void_p]
        self.library.cudaHostAlloc.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_size_t,
            ctypes.c_uint,
        ]
        self.library.cudaFreeHost.argtypes = [ctypes.c_void_p]
        self.library.cudaStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.library.cudaStreamDestroy.argtypes = [ctypes.c_void_p]
        self.library.cudaStreamSynchronize.argtypes = [ctypes.c_void_p]
        self.library.cudaMemcpyAsync.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_void_p,
        ]

    def check(self, status: int, operation: str) -> None:
        if status == 0:
            return
        encoded = self.library.cudaGetErrorString(status)
        detail = encoded.decode("utf-8", errors="replace") if encoded else str(status)
        raise TensorRTInferenceError(f"{operation} failed: {detail}")

    def device_alloc(self, size: int) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        self.check(self.library.cudaMalloc(ctypes.byref(pointer), size), "cudaMalloc")
        return pointer

    def pinned_alloc(self, count: int) -> tuple[ctypes.c_void_p, np.ndarray]:
        pointer = ctypes.c_void_p()
        size = count * np.dtype(np.float32).itemsize
        self.check(
            self.library.cudaHostAlloc(ctypes.byref(pointer), size, 0),
            "cudaHostAlloc",
        )
        values = (ctypes.c_float * count).from_address(pointer.value)
        return pointer, np.ctypeslib.as_array(values)

    def stream_create(self) -> ctypes.c_void_p:
        stream = ctypes.c_void_p()
        self.check(
            self.library.cudaStreamCreate(ctypes.byref(stream)),
            "cudaStreamCreate",
        )
        return stream


class TensorRTStereoResNet18:
    """Execute one same-frame crop batch without runtime allocation."""

    def __init__(self, engine_path: Path | str) -> None:
        try:
            import tensorrt as trt
        except ImportError as exc:
            raise TensorRTInferenceError("TensorRT Python bindings are unavailable") from exc
        self._trt = trt
        self.engine_path = Path(engine_path).expanduser().resolve()
        if not self.engine_path.is_file():
            raise TensorRTInferenceError(
                f"TensorRT engine does not exist: {self.engine_path}"
            )
        self._cuda = _CudaRuntime()
        self._logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(self._logger)
        self._engine = self._runtime.deserialize_cuda_engine(
            self.engine_path.read_bytes()
        )
        if self._engine is None:
            raise TensorRTInferenceError("could not deserialize TensorRT engine")
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise TensorRTInferenceError("could not create TensorRT execution context")
        names = {
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
        }
        required = {"CamL", "CamR", "logits"}
        if not required.issubset(names):
            raise TensorRTInferenceError(
                f"stereo engine tensors are {sorted(names)}, expected {sorted(required)}"
            )
        maximum = self._engine.get_tensor_profile_shape("CamL", 0)[2]
        self.max_batch = int(maximum[0])
        output_shape = tuple(int(value) for value in self._engine.get_tensor_shape("logits"))
        self.class_count = output_shape[-1]
        if self.max_batch <= 0 or self.class_count <= 0:
            raise TensorRTInferenceError("TensorRT engine reports invalid tensor shapes")

        self._input_count = self.max_batch * 3 * 224 * 224
        self._output_count = self.max_batch * self.class_count
        self._left_pointer, left = self._cuda.pinned_alloc(self._input_count)
        self._right_pointer, right = self._cuda.pinned_alloc(self._input_count)
        self._output_pointer, output = self._cuda.pinned_alloc(self._output_count)
        self._left = left.reshape(self.max_batch, 3, 224, 224)
        self._right = right.reshape(self.max_batch, 3, 224, 224)
        self._output = output.reshape(self.max_batch, self.class_count)
        input_bytes = self._input_count * np.dtype(np.float32).itemsize
        output_bytes = self._output_count * np.dtype(np.float32).itemsize
        self._device_left = self._cuda.device_alloc(input_bytes)
        self._device_right = self._cuda.device_alloc(input_bytes)
        self._device_output = self._cuda.device_alloc(output_bytes)
        self._stream = self._cuda.stream_create()
        self._preprocess_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="beano-camr-preprocess",
        )
        self._closed = False

    def infer(
        self,
        caml_images: tuple[np.ndarray, ...],
        camr_images: tuple[np.ndarray, ...] | None = None,
    ) -> TensorRTBatchResult:
        batch = len(caml_images)
        if not 1 <= batch <= self.max_batch:
            raise TensorRTInferenceError(
                f"TensorRT batch must contain 1-{self.max_batch} crops"
            )
        if camr_images is not None and len(camr_images) != batch:
            raise TensorRTInferenceError("CamL and CamR batch sizes differ")
        started_ns = time.perf_counter_ns()
        right_future = None
        if camr_images is None:
            _prepare_batch(caml_images, self._left)
            np.copyto(self._right[:batch], self._left[:batch])
        else:
            # The two independent camera towers write disjoint pinned buffers.
            # Prepare CamR concurrently with CamL so genuine stereo does not
            # serialize twice the CPU normalization work.
            right_future = self._preprocess_pool.submit(
                _prepare_batch,
                camr_images,
                self._right,
            )
            _prepare_batch(caml_images, self._left)
            right_future.result()
        prepared_ns = time.perf_counter_ns()
        input_bytes = batch * 3 * 224 * 224 * np.dtype(np.float32).itemsize
        output_bytes = batch * self.class_count * np.dtype(np.float32).itemsize
        cuda = self._cuda.library
        stream = self._stream
        self._cuda.check(
            cuda.cudaMemcpyAsync(
                self._device_left,
                self._left_pointer,
                input_bytes,
                _CudaRuntime.HOST_TO_DEVICE,
                stream,
            ),
            "CamL cudaMemcpyAsync",
        )
        self._cuda.check(
            cuda.cudaMemcpyAsync(
                self._device_right,
                self._right_pointer,
                input_bytes,
                _CudaRuntime.HOST_TO_DEVICE,
                stream,
            ),
            "CamR cudaMemcpyAsync",
        )
        shape = (batch, 3, 224, 224)
        if not self._context.set_input_shape("CamL", shape):
            raise TensorRTInferenceError("TensorRT rejected CamL input shape")
        if not self._context.set_input_shape("CamR", shape):
            raise TensorRTInferenceError("TensorRT rejected CamR input shape")
        self._context.set_tensor_address("CamL", int(self._device_left.value))
        self._context.set_tensor_address("CamR", int(self._device_right.value))
        self._context.set_tensor_address("logits", int(self._device_output.value))
        if not self._context.execute_async_v3(stream_handle=int(stream.value)):
            raise TensorRTInferenceError("TensorRT enqueueV3 failed")
        self._cuda.check(
            cuda.cudaMemcpyAsync(
                self._output_pointer,
                self._device_output,
                output_bytes,
                _CudaRuntime.DEVICE_TO_HOST,
                stream,
            ),
            "logit cudaMemcpyAsync",
        )
        self._cuda.check(cuda.cudaStreamSynchronize(stream), "cudaStreamSynchronize")
        completed_ns = time.perf_counter_ns()
        return TensorRTBatchResult(
            tuple(tuple(float(value) for value in row) for row in self._output[:batch]),
            (prepared_ns - started_ns) / 1_000_000.0,
            (completed_ns - prepared_ns) / 1_000_000.0,
            (completed_ns - started_ns) / 1_000_000.0,
        )

    def warm_up(self) -> None:
        """Pay lazy CUDA/TensorRT setup costs before accepting live crops."""

        image = np.zeros((224, 224, 3), dtype=np.uint8)
        for batch in range(1, self.max_batch + 1):
            images = (image,) * batch
            self.infer(images, images)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._preprocess_pool.shutdown(wait=True, cancel_futures=True)
        cuda = self._cuda.library
        cuda.cudaStreamDestroy(self._stream)
        for pointer in (self._device_left, self._device_right, self._device_output):
            cuda.cudaFree(pointer)
        for pointer in (
            self._left_pointer,
            self._right_pointer,
            self._output_pointer,
        ):
            cuda.cudaFreeHost(pointer)


def _prepare_rgb_chw(image_bgr: np.ndarray, destination: np.ndarray) -> None:
    if image_bgr.dtype != np.uint8 or image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise TensorRTInferenceError("inference crop must be an 8-bit BGR image")
    image = (
        image_bgr
        if image_bgr.shape[:2] == (224, 224)
        else cv2.resize(image_bgr, (224, 224), interpolation=cv2.INTER_LINEAR)
    )
    # The mock-trained model intentionally uses only [0,1] scaling.
    np.multiply(image[:, :, 2], 1.0 / 255.0, out=destination[0], casting="unsafe")
    np.multiply(image[:, :, 1], 1.0 / 255.0, out=destination[1], casting="unsafe")
    np.multiply(image[:, :, 0], 1.0 / 255.0, out=destination[2], casting="unsafe")


def _prepare_batch(images: tuple[np.ndarray, ...], destination: np.ndarray) -> None:
    for index, image in enumerate(images):
        _prepare_rgb_chw(image, destination[index])
