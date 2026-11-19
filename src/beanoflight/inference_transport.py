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
from .sorting_context_transport import (
    sorting_context_from_dict,
    sorting_context_to_dict,
)
from .stereo import StereoPairMetadata

CROP_SCHEMA = "beanoflight-crop/v3"
BATCH_CROP_SCHEMA = "beanoflight-crop-batch/v3"
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
        views = _validated_views(payload)
        request_id = uuid.uuid4().hex
        header = {
            "schema": CROP_SCHEMA,
            "request_id": request_id,
            "pixel_format": "BGR8",
            "shapes": [list(image.shape) for image in views],
            "stereo_pair": (
                None
                if payload.stereo_pair is None
                else payload.stereo_pair.to_json()
            ),
            "sorting_context": (
                None
                if payload.sorting_context is None
                else sorting_context_to_dict(payload.sorting_context)
            ),
            "job": inference_job_to_dict(payload.job),
        }
        self._send(request_id, CROP_SCHEMA, header, views)

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
        views = tuple(_validated_views(payload) for payload in payloads)
        contiguous = tuple(image for item in views for image in item)
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
                    "shapes": [list(image.shape) for image in item_views],
                    "stereo_pair": (
                        None
                        if payload.stereo_pair is None
                        else payload.stereo_pair.to_json()
                    ),
                    "sorting_context": (
                        None
                        if payload.sorting_context is None
                        else sorting_context_to_dict(payload.sorting_context)
                    ),
                    "job": inference_job_to_dict(payload.job),
                }
                for payload, item_views in zip(payloads, views)
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
                (_encode(header), *(memoryview(image) for image in images)),
                copy=False,
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
            parts = self.socket.recv_multipart(copy=False)
            if len(parts) < 2:
                raise CropTransportError("crop request must contain image data")
            header = _object(json.loads(bytes(parts[0]).decode("utf-8")))
            request_id = str(header.get("request_id", ""))
            schema = str(header.get("schema", ""))
            if schema not in {CROP_SCHEMA, BATCH_CROP_SCHEMA} or not request_id:
                raise CropTransportError("invalid crop request schema")
            if header.get("pixel_format") != "BGR8":
                raise CropTransportError("unsupported crop pixel format")
            if schema == CROP_SCHEMA:
                shapes = _array(header.get("shapes"))
                if len(parts) != len(shapes) + 1:
                    raise CropTransportError("single crop view count does not match")
                payloads = (
                    _decode_payload(
                        header.get("job"),
                        shapes,
                        parts[1:],
                        header.get("stereo_pair"),
                        header.get("sorting_context"),
                    ),
                )
            else:
                items = _array(header.get("items"))
                if not 1 <= len(items) <= MAX_BATCH_CROPS:
                    raise CropTransportError("crop batch item count does not match")
                decoded = []
                offset = 1
                for item_value in items:
                    item = _object(item_value)
                    shapes = _array(item.get("shapes"))
                    next_offset = offset + len(shapes)
                    if len(shapes) not in {1, 2} or next_offset > len(parts):
                        raise CropTransportError(
                            "crop batch view count does not match"
                        )
                    decoded.append(
                        _decode_payload(
                            item.get("job"),
                            shapes,
                            parts[offset:next_offset],
                            item.get("stereo_pair"),
                            item.get("sorting_context"),
                        )
                    )
                    offset = next_offset
                if offset != len(parts):
                    raise CropTransportError("crop batch contains extra image data")
                payloads = tuple(decoded)
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


def _validated_views(payload: CropPayload) -> tuple[np.ndarray, ...]:
    left = _validated_image(payload.image_bgr, payload, "CamL")
    right = payload.camr_image_bgr
    if payload.stereo_pair is None:
        if right is not None:
            raise CropTransportError("CamR crop has no stereo pair metadata")
        return (left,)
    if right is None:
        raise CropTransportError("stereo crop is missing its CamR view")
    return left, _validated_image(right, payload, "CamR")


def _validated_image(
    image: object, payload: CropPayload, camera: str
) -> np.ndarray:
    if (
        not isinstance(image, np.ndarray)
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
        or image.shape[0] != payload.job.crop_height_px
        or image.shape[1] != payload.job.crop_width_px
    ):
        raise CropTransportError(
            f"{camera} crop must be a matching uint8 BGR image"
        )
    contiguous = np.ascontiguousarray(image)
    if contiguous.nbytes > MAX_CROP_BYTES:
        raise CropTransportError("crop exceeds transport size limit")
    return contiguous


def _decode_payload(
    job_value: object,
    shape_values: list[object],
    data_values: list[object],
    pair_value: object,
    sorting_context_value: object,
) -> CropPayload:
    if len(shape_values) not in {1, 2} or len(data_values) != len(shape_values):
        raise CropTransportError("crop must contain one or two views")
    job = inference_job_from_dict(_object(job_value))
    images = []
    for shape_value, data in zip(shape_values, data_values):
        shape = tuple(int(value) for value in _array(shape_value))
        if len(shape) != 3 or shape[2] != 3 or min(shape) <= 0:
            raise CropTransportError("invalid crop shape")
        expected = shape[0] * shape[1] * shape[2]
        if expected != len(data) or expected > MAX_CROP_BYTES:
            raise CropTransportError("crop byte count does not match shape")
        if (job.crop_height_px, job.crop_width_px, 3) != shape:
            raise CropTransportError("crop shape does not match job metadata")
        buffer = data.buffer if isinstance(data, zmq.Frame) else data
        images.append(np.frombuffer(buffer, dtype=np.uint8).reshape(shape))
    if len(images) == 2:
        if not isinstance(pair_value, dict):
            raise CropTransportError("stereo crop has no pair metadata")
        try:
            pair = StereoPairMetadata.from_json(pair_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise CropTransportError(f"invalid stereo pair metadata: {exc}") from exc
    else:
        if pair_value is not None:
            raise CropTransportError("single-view crop contains stereo metadata")
        pair = None
    sorting_context = (
        None
        if sorting_context_value is None
        else sorting_context_from_dict(sorting_context_value)
    )
    if (
        sorting_context is not None
        and sorting_context.track.bean_ref != job.bean_ref
    ):
        raise CropTransportError("sorting context does not match crop job")
    return CropPayload(
        job,
        images[0],
        None,
        None if len(images) == 1 else images[1],
        None,
        pair,
        sorting_context,
    )


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
