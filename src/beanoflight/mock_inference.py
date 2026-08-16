"""Deterministic delayed classifier used to exercise asynchronous inference."""

from __future__ import annotations

import hashlib
import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .crop import CropPayload
from .inference_transport import DEFAULT_CROP_ENDPOINT, ZeroMQCropReceiver
from .registry_models import Enrichment, InferenceStatus
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient


@dataclass(frozen=True, slots=True)
class MockInferenceSettings:
    latency_ms: float = 25.0
    jitter_ms: float = 5.0
    worker_count: int = 1
    queue_capacity: int = 32
    seed: int = 7
    categories: tuple[str, ...] = ("acceptable", "insect_damage", "mould", "broken")
    weights: tuple[float, ...] = (0.65, 0.15, 0.10, 0.10)
    confidence_min: float = 0.70
    confidence_max: float = 0.99

    def validate(self) -> None:
        if (
            not math.isfinite(self.latency_ms)
            or not math.isfinite(self.jitter_ms)
            or self.latency_ms < 0
            or self.jitter_ms < 0
        ):
            raise ValueError("mock inference latency must be finite and non-negative")
        if self.worker_count <= 0 or self.queue_capacity <= 0:
            raise ValueError("mock worker and queue counts must be positive")
        if not self.categories or len(self.categories) != len(self.weights):
            raise ValueError(
                "mock categories and weights must have equal non-zero length"
            )
        if any(not category.strip() for category in self.categories):
            raise ValueError("mock categories cannot be blank")
        if (
            any(not math.isfinite(weight) or weight < 0 for weight in self.weights)
            or sum(self.weights) <= 0
        ):
            raise ValueError(
                "mock category weights must be non-negative with a positive sum"
            )
        if (
            not math.isfinite(self.confidence_min)
            or not math.isfinite(self.confidence_max)
            or not 0 <= self.confidence_min <= self.confidence_max <= 1
        ):
            raise ValueError("mock confidence range must be between zero and one")


@dataclass(frozen=True, slots=True)
class MockInferenceActivity:
    kind: str
    job_id: str
    bean_id: str
    category: str = ""
    confidence: float | None = None
    detail: str = ""
    crop: object | None = None


class MockInferencerService:
    """Receive crops, simulate bounded worker latency, and enrich BeanRegistry."""

    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        crop_endpoint: str = DEFAULT_CROP_ENDPOINT,
        settings: MockInferenceSettings | None = None,
        activity: Callable[[MockInferenceActivity], None] | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.crop_endpoint = crop_endpoint
        self.settings = settings or MockInferenceSettings()
        self.settings.validate()
        self.activity = activity
        self._queue: queue.Queue[CropPayload] = queue.Queue(
            maxsize=self.settings.queue_capacity
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.ready = threading.Event()
        self.received = 0
        self.completed = 0
        self.dropped = 0
        self.startup_error = ""

    def start(self) -> None:
        if self._threads:
            return
        receiver = threading.Thread(
            target=self._receive_loop,
            name="beano-mock-inferencer-receiver",
            daemon=True,
        )
        self._threads.append(receiver)
        receiver.start()
        for index in range(self.settings.worker_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"beano-mock-inferencer-{index}",
                daemon=True,
            )
            self._threads.append(worker)
            worker.start()

    def close(self, *, drain: bool = True) -> None:
        if drain:
            self._queue.join()
        self._stop.set()
        for thread in self._threads:
            thread.join(2.0)
        self._threads.clear()

    def _receive_loop(self) -> None:
        try:
            receiver = ZeroMQCropReceiver(
                self.crop_endpoint, capacity=self.settings.queue_capacity
            )
        except Exception as exc:  # noqa: BLE001 - reported to controller GUI
            self.startup_error = str(exc)
            self._emit("error", detail=str(exc))
            self.ready.set()
            return
        self.crop_endpoint = receiver.endpoint
        self.ready.set()
        try:
            while not self._stop.is_set():
                receiver.receive(timeout_ms=100, accept=self._accept_payload)
        finally:
            receiver.close()

    def _accept_payload(self, payload: CropPayload) -> bool:
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1
            self._emit("rejected", payload, detail="mock inference queue full")
            return False
        self.received += 1
        self._emit("received", payload, crop=payload.image_bgr)
        return True

    def _worker_loop(self) -> None:
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    payload = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    randomizer = _job_randomizer(self.settings.seed, payload.job.job_id)
                    delay_ms = max(
                        0.0,
                        randomizer.uniform(
                            self.settings.latency_ms - self.settings.jitter_ms,
                            self.settings.latency_ms + self.settings.jitter_ms,
                        ),
                    )
                    if self._stop.wait(delay_ms / 1_000.0):
                        self._mark_failed(payload, "mock inferencer stopped")
                        continue
                    category = randomizer.choices(
                        self.settings.categories,
                        weights=self.settings.weights,
                        k=1,
                    )[0]
                    confidence = randomizer.uniform(
                        self.settings.confidence_min,
                        self.settings.confidence_max,
                    )
                    session = registry.get_session(payload.job.bean_ref.run_id)
                    result_timestamp = max(
                        payload.job.capture_timestamp_ns,
                        session.monotonic_to_source_ns(time.monotonic_ns()),
                    )
                    enrichment = Enrichment(
                        source="mock-inferencer",
                        kind="classification",
                        value={"category": category, "job_id": payload.job.job_id},
                        timestamp_ns=result_timestamp,
                        version="mock-resnet-v1",
                        result_id=payload.job.job_id,
                        confidence=confidence,
                    )
                    registry.complete_inference_job(
                        payload.job.bean_ref,
                        payload.job.job_id,
                        enrichment,
                        event_id=f"complete:{payload.job.job_id}",
                    )
                    self.completed += 1
                    self._emit(
                        "completed",
                        payload,
                        category=category,
                        confidence=confidence,
                    )
                except Exception as exc:  # noqa: BLE001 - job failure is observable state
                    self.dropped += 1
                    self._mark_failed(payload, str(exc), registry=registry)
                finally:
                    self._queue.task_done()
        finally:
            registry.close()

    def _mark_failed(
        self,
        payload: CropPayload,
        detail: str,
        *,
        registry: ZeroMQRegistryClient | None = None,
    ) -> None:
        own_client = registry is None
        client = registry or ZeroMQRegistryClient(
            self.registry_endpoint, timeout_ms=2_000
        )
        try:
            client.update_inference_job(
                payload.job.bean_ref,
                payload.job.job_id,
                InferenceStatus.FAILED,
                payload.job.capture_timestamp_ns,
                detail=detail,
                event_id=f"fail:{payload.job.job_id}",
            )
        except Exception:  # noqa: BLE001, S110 - original error is already reported
            pass
        finally:
            if own_client:
                client.close()
        self._emit("failed", payload, detail=detail)

    def _emit(
        self,
        kind: str,
        payload: CropPayload | None = None,
        *,
        category: str = "",
        confidence: float | None = None,
        detail: str = "",
        crop: object | None = None,
    ) -> None:
        if self.activity is None:
            return
        self.activity(
            MockInferenceActivity(
                kind,
                "" if payload is None else payload.job.job_id,
                "" if payload is None else str(payload.job.bean_ref),
                category,
                confidence,
                detail,
                crop,
            )
        )


def _job_randomizer(seed: int, job_id: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{job_id}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))
