"""Acknowledged lossless crop transport for external inference workers."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable

import numpy as np
import zmq

from .crop import CropPayload
from .registry_models import inference_job_from_dict, inference_job_to_dict

CROP_SCHEMA = "beanoflight-crop/v1"
BATCH_CROP_SCHEMA = "beanoflight-crop-batch/v1"
MAX_CROP_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_BATCH_CROPS = 16
DEFAULT_CROP_ENDPOINT = "ipc:///tmp/beanoflight-inference-crops.ipc"


class CropTransportError(RuntimeError):
    pass


class ZeroMQCropClient:
    """Thread-affine acknowledged crop sender."""

    def __init__(
        self,
        endpoint: str = DEFAULT_CROP_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        timeout_ms: int = 1_000,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.timeout_ms = max(1, int(timeout_ms))
        self._socket: zmq.Socket | None = None
        self._thread_id: int | None = None

    def submit(self, payload: CropPayload) -> None:
        self._claim_thread()
        contiguous = _validated_image(payload)
        request_id = uuid.uuid4().hex
        header = {
            "schema": CROP_SCHEMA,
            "request_id": request_id,
            "pixel_format": "BGR8",
            "shape": list(contiguous.shape),
            "job": inference_job_to_dict(payload.job),
        }
        self._send(request_id, CROP_SCHEMA, header, (contiguous,))

    def submit_batch(self, payloads: tuple[CropPayload, ...]) -> None:
        """Submit every selected crop from one source frame atomically."""
        self._claim_thread()
        if not 1 <= len(payloads) <= MAX_BATCH_CROPS:
            raise CropTransportError(
                f"crop batch must contain between 1 and {MAX_BATCH_CROPS} crops"
            )
        run_ids = {payload.job.bean_ref.run_id for payload in payloads}
        camera_ids = {payload.job.camera_id for payload in payloads}
        frame_indices = {payload.job.frame_index for payload in payloads}
        if len(run_ids) != 1 or len(camera_ids) != 1 or len(frame_indices) != 1:
            raise CropTransportError("crop batch jobs must share run, camera, and frame")
        contiguous = tuple(_validated_image(payload) for payload in payloads)
        if sum(image.nbytes for image in contiguous) > MAX_BATCH_BYTES:
            raise CropTransportError("crop batch exceeds transport size limit")
        request_id = uuid.uuid4().hex
        jobs = tuple(payload.job for payload in payloads)
        header = {
            "schema": BATCH_CROP_SCHEMA,
            "request_id": request_id,
            "batch_id": _frame_batch_id(jobs),
            "pixel_format": "BGR8",
            "items": [
                {
                    "shape": list(image.shape),
                    "job": inference_job_to_dict(payload.job),
                }
                for payload, image in zip(payloads, contiguous)
            ],
        }
        self._send(request_id, BATCH_CROP_SCHEMA, header, contiguous)

    def _claim_thread(self) -> None:
        current_thread = threading.get_ident()
        if self._thread_id is None:
            self._thread_id = current_thread
        elif self._thread_id != current_thread:
            raise CropTransportError("create one crop client per sending thread")

    def _send(
        self,
        request_id: str,
        schema: str,
        header: dict[str, object],
        images: tuple[np.ndarray, ...],
    ) -> None:
        socket = self._ensure_socket()
        try:
            socket.send_multipart(
                (_encode(header), *(memoryview(image) for image in images))
            )
            if not socket.poll(self.timeout_ms, zmq.POLLIN):
                raise CropTransportError(
                    f"inferencer did not accept crop within {self.timeout_ms} ms"
                )
            response = _object(json.loads(socket.recv().decode("utf-8")))
        except Exception:
            self._reset_socket()
            raise
        if response.get("schema") != schema:
            raise CropTransportError("invalid crop acknowledgement schema")
        if response.get("request_id") != request_id:
            raise CropTransportError("crop acknowledgement ID does not match")
        if not response.get("accepted"):
            raise CropTransportError(str(response.get("message", "crop rejected")))

    def close(self) -> None:
        self._reset_socket()

    def _ensure_socket(self) -> zmq.Socket:
        if self._socket is None:
            socket = self.context.socket(zmq.REQ)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.MAXMSGSIZE, MAX_BATCH_BYTES)
            socket.setsockopt(zmq.SNDHWM, 16)
            socket.setsockopt(zmq.RCVHWM, 16)
            socket.connect(self.endpoint)
            self._socket = socket
        return self._socket

    def _reset_socket(self) -> None:
        if self._socket is not None:
            self._socket.close(0)
            self._socket = None


class ZeroMQCropReceiver:
    """Receive and acknowledge complete crop jobs without retaining history."""

    def __init__(
        self,
        endpoint: str = DEFAULT_CROP_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 32,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.socket = self.context.socket(zmq.REP)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_BATCH_BYTES)
        self.socket.setsockopt(zmq.RCVHWM, max(1, int(capacity)))
        self.socket.setsockopt(zmq.SNDHWM, max(1, int(capacity)))
        self.socket.bind(endpoint)
        self.endpoint = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)

    def receive(
        self,
        *,
        timeout_ms: int = 100,
        accept: Callable[[CropPayload], bool] | None = None,
    ) -> CropPayload | None:
        batch = self._receive_batch(
            timeout_ms=timeout_ms,
            accept=(
                None
                if accept is None
                else lambda payloads: len(payloads) == 1 and accept(payloads[0])
            ),
        )
        return None if batch is None or len(batch) != 1 else batch[0]

    def receive_batch(
        self,
        *,
        timeout_ms: int = 100,
        accept: Callable[[tuple[CropPayload, ...]], bool] | None = None,
    ) -> tuple[CropPayload, ...] | None:
        """Receive either an explicit frame batch or a legacy singleton."""
        return self._receive_batch(timeout_ms=timeout_ms, accept=accept)

    def _receive_batch(
        self,
        *,
        timeout_ms: int,
        accept: Callable[[tuple[CropPayload, ...]], bool] | None,
    ) -> tuple[CropPayload, ...] | None:
        if not self.socket.poll(max(0, int(timeout_ms)), zmq.POLLIN):
            return None
        request_id = ""
        schema = CROP_SCHEMA
        try:
            parts = self.socket.recv_multipart()
            if len(parts) < 2:
                raise CropTransportError("crop request must contain image data")
            header = _object(json.loads(parts[0].decode("utf-8")))
            request_id = str(header.get("request_id", ""))
            schema = str(header.get("schema", ""))
            if schema not in {CROP_SCHEMA, BATCH_CROP_SCHEMA} or not request_id:
                raise CropTransportError("invalid crop request schema")
            if header.get("pixel_format") != "BGR8":
                raise CropTransportError("unsupported crop pixel format")
            if schema == CROP_SCHEMA:
                if len(parts) != 2:
                    raise CropTransportError(
                        "single crop request must contain one image"
                    )
                payloads = (
                    _decode_payload(header.get("job"), header.get("shape"), parts[1]),
                )
            else:
                items = _array(header.get("items"))
                if not 1 <= len(items) <= MAX_BATCH_CROPS or len(parts) != len(items) + 1:
                    raise CropTransportError("crop batch item count does not match")
                payloads = tuple(
                    _decode_payload(
                        _object(item).get("job"),
                        _object(item).get("shape"),
                        image,
                    )
                    for item, image in zip(items, parts[1:])
                )
                jobs = tuple(payload.job for payload in payloads)
                if header.get("batch_id") != _frame_batch_id(jobs):
                    raise CropTransportError("crop batch ID or membership is invalid")
            if accept is not None and not accept(payloads):
                raise CropTransportError("inferencer queue is full")
            response = {
                "schema": schema,
                "request_id": request_id,
                "accepted": True,
            }
        except Exception as exc:  # noqa: BLE001 - REP must always answer
            payloads = None
            response = {
                "schema": schema,
                "request_id": request_id,
                "accepted": False,
                "message": str(exc),
            }
        self.socket.send(_encode(response))
        return payloads

    def close(self) -> None:
        self.socket.close(0)

    def __enter__(self) -> ZeroMQCropReceiver:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def _encode(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CropTransportError("crop message value must be an object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise CropTransportError("crop message value must be an array")
    return value


def _validated_image(payload: CropPayload) -> np.ndarray:
    image = payload.image_bgr
    if (
        not isinstance(image, np.ndarray)
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
        or image.shape[0] != payload.job.crop_height_px
        or image.shape[1] != payload.job.crop_width_px
    ):
        raise CropTransportError("crop must be a matching uint8 BGR image")
    contiguous = np.ascontiguousarray(image)
    if contiguous.nbytes > MAX_CROP_BYTES:
        raise CropTransportError("crop exceeds transport size limit")
    return contiguous


def _decode_payload(job_value: object, shape_value: object, data: bytes) -> CropPayload:
    shape = tuple(int(value) for value in _array(shape_value))
    if len(shape) != 3 or shape[2] != 3 or min(shape) <= 0:
        raise CropTransportError("invalid crop shape")
    expected = shape[0] * shape[1] * shape[2]
    if expected != len(data) or expected > MAX_CROP_BYTES:
        raise CropTransportError("crop byte count does not match shape")
    job = inference_job_from_dict(_object(job_value))
    if (job.crop_height_px, job.crop_width_px, 3) != shape:
        raise CropTransportError("crop shape does not match job metadata")
    image = np.frombuffer(data, dtype=np.uint8).reshape(shape).copy()
    return CropPayload(job, image)


def _frame_batch_id(jobs) -> str:
    jobs = tuple(jobs)
    if not jobs:
        raise CropTransportError("crop batch cannot be empty")
    run_ids = {job.bean_ref.run_id for job in jobs}
    camera_ids = {job.camera_id for job in jobs}
    frame_indices = {job.frame_index for job in jobs}
    if len(run_ids) != 1 or len(camera_ids) != 1 or len(frame_indices) != 1:
        raise CropTransportError("crop batch jobs must share run, camera, and frame")
    return ":".join(
        (
            "frame",
            next(iter(run_ids)),
            next(iter(camera_ids)),
            str(next(iter(frame_indices))),
        )
    )
