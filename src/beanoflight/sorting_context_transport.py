"""Best-effort real-time trajectory context transport to BeanoSorter."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import zmq

from .models import CrossingPrediction, TrackSnapshot
from .registry_models import (
    prediction_from_dict,
    prediction_to_dict,
    track_from_dict,
    track_to_dict,
)

SORTING_CONTEXT_SCHEMA = "beanoflight-sorting-context/v1"
DEFAULT_SORTING_CONTEXT_ENDPOINT = "ipc:///tmp/beanoflight-sorting-context.ipc"
MAX_SORTING_CONTEXT_BYTES = 1024 * 1024
MAX_SORTING_CONTEXT_ITEMS = 32


class SortingContextTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SortingContext:
    track: TrackSnapshot
    prediction: CrossingPrediction | None


def sorting_context_to_dict(context: SortingContext) -> dict[str, object]:
    """Serialize one context for reuse by the crop and evidence transports."""

    return {
        "track": track_to_dict(context.track, include_history=False),
        "prediction": (
            None
            if context.prediction is None
            else prediction_to_dict(context.prediction)
        ),
    }


def sorting_context_from_dict(value: object) -> SortingContext:
    """Deserialize and validate one context shared with a bean crop."""

    raw = _object(value)
    track = track_from_dict(_object(raw.get("track")))
    prediction_value = raw.get("prediction")
    prediction = (
        None
        if prediction_value is None
        else prediction_from_dict(_object(prediction_value))
    )
    if prediction is not None and prediction.bean_ref != track.bean_ref:
        raise SortingContextTransportError(
            "sorting prediction does not match its track"
        )
    return SortingContext(track, prediction)


@dataclass(frozen=True, slots=True)
class SortingContextBatch:
    run_id: str
    frame_index: int
    source_fps: float
    target_fps: float
    clock_source_timestamp_ns: int
    clock_monotonic_ns: int
    clock_epoch: int
    sent_monotonic_ns: int
    items: tuple[SortingContext, ...]


class ZeroMQSortingContextPublisher:
    """Non-blocking producer; Registry state recovers any dropped context."""

    def __init__(
        self,
        endpoint: str = DEFAULT_SORTING_CONTEXT_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 64,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_SORTING_CONTEXT_BYTES)
        self.socket.setsockopt(zmq.SNDHWM, max(1, int(capacity)))
        self.socket.connect(endpoint)
        self.batches_sent = 0
        self.batches_dropped = 0
        self.contexts_sent = 0
        self.contexts_dropped = 0

    def send_batch(
        self,
        *,
        run_id: str,
        frame_index: int,
        source_fps: float,
        target_fps: float,
        clock_source_timestamp_ns: int,
        clock_monotonic_ns: int,
        clock_epoch: int,
        items: tuple[SortingContext, ...],
    ) -> bool:
        if not items:
            return True
        if not 1 <= len(items) <= MAX_SORTING_CONTEXT_ITEMS:
            raise SortingContextTransportError(
                "sorting context batch must contain between 1 and 32 items"
            )
        if not run_id or source_fps <= 0 or target_fps < 0 or clock_epoch <= 0:
            raise SortingContextTransportError("invalid sorting context clock")
        for item in items:
            if item.track.bean_ref.run_id != run_id:
                raise SortingContextTransportError(
                    "sorting context tracks must belong to the batch run"
                )
            if (
                item.prediction is not None
                and item.prediction.bean_ref != item.track.bean_ref
            ):
                raise SortingContextTransportError(
                    "sorting prediction does not match its track"
                )
        sent_ns = time.monotonic_ns()
        encoded = _encode(
            {
                "schema": SORTING_CONTEXT_SCHEMA,
                "run_id": run_id,
                "frame_index": int(frame_index),
                "source_fps": float(source_fps),
                "target_fps": float(target_fps),
                "clock_source_timestamp_ns": int(clock_source_timestamp_ns),
                "clock_monotonic_ns": int(clock_monotonic_ns),
                "clock_epoch": int(clock_epoch),
                "sent_monotonic_ns": sent_ns,
                "items": [sorting_context_to_dict(item) for item in items],
            }
        )
        if len(encoded) > MAX_SORTING_CONTEXT_BYTES:
            raise SortingContextTransportError(
                "sorting context batch exceeds transport size limit"
            )
        try:
            self.socket.send(encoded, flags=zmq.NOBLOCK)
        except zmq.Again:
            self.batches_dropped += 1
            self.contexts_dropped += len(items)
            return False
        self.batches_sent += 1
        self.contexts_sent += len(items)
        return True

    def statistics(self) -> dict[str, int]:
        return {
            "batches_sent": self.batches_sent,
            "batches_dropped": self.batches_dropped,
            "contexts_sent": self.contexts_sent,
            "contexts_dropped": self.contexts_dropped,
        }

    def close(self) -> None:
        self.socket.close(0)


class ZeroMQSortingContextReceiver:
    """PULL consumer owned by the single real-time sorter process."""

    def __init__(
        self,
        endpoint: str = DEFAULT_SORTING_CONTEXT_ENDPOINT,
        *,
        context: zmq.Context | None = None,
        capacity: int = 64,
    ) -> None:
        self.endpoint = endpoint
        self.context = context or zmq.Context.instance()
        self.socket = self.context.socket(zmq.PULL)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_SORTING_CONTEXT_BYTES)
        self.socket.setsockopt(zmq.RCVHWM, max(1, int(capacity)))
        self.socket.bind(endpoint)
        self.endpoint = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)

    def receive_batch(self) -> SortingContextBatch:
        encoded = self.socket.recv()
        if len(encoded) > MAX_SORTING_CONTEXT_BYTES:
            raise SortingContextTransportError(
                "sorting context batch exceeds transport size limit"
            )
        message = _object(json.loads(encoded.decode("utf-8")))
        if message.get("schema") != SORTING_CONTEXT_SCHEMA:
            raise SortingContextTransportError("invalid sorting context schema")
        raw_items = _array(message.get("items"))
        if not 1 <= len(raw_items) <= MAX_SORTING_CONTEXT_ITEMS:
            raise SortingContextTransportError("invalid sorting context batch")
        items = [sorting_context_from_dict(raw) for raw in raw_items]
        result = SortingContextBatch(
            run_id=str(message.get("run_id", "")),
            frame_index=int(message.get("frame_index", -1)),
            source_fps=float(message.get("source_fps", 0.0)),
            target_fps=float(message.get("target_fps", -1.0)),
            clock_source_timestamp_ns=int(
                message.get("clock_source_timestamp_ns", -1)
            ),
            clock_monotonic_ns=int(message.get("clock_monotonic_ns", -1)),
            clock_epoch=int(message.get("clock_epoch", -1)),
            sent_monotonic_ns=int(message.get("sent_monotonic_ns", -1)),
            items=tuple(items),
        )
        if (
            not result.run_id
            or result.frame_index < 0
            or result.source_fps <= 0
            or result.target_fps < 0
            or result.clock_source_timestamp_ns < 0
            or result.clock_monotonic_ns <= 0
            or result.clock_epoch <= 0
            or result.sent_monotonic_ns <= 0
            or any(item.track.bean_ref.run_id != result.run_id for item in items)
        ):
            raise SortingContextTransportError("invalid sorting context metadata")
        return result

    def close(self) -> None:
        self.socket.close(0)


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SortingContextTransportError(
            "sorting context message value must be an object"
        )
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise SortingContextTransportError(
            "sorting context message value must be an array"
        )
    return value
