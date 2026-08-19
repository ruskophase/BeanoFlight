"""Deterministic, batched classifier used to exercise asynchronous inference."""

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
from .registry_models import Enrichment, InferenceJob, InferenceStatus
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient

# Conservative TensorRT FP16 estimates for a shared ResNet18 backbone receiving
# two views per bean. The x axis is the number of images in one GPU batch.
DEFAULT_STEREO_LATENCY_CURVE: tuple[tuple[int, float], ...] = (
    (2, 15.0),
    (4, 18.0),
    (8, 23.0),
    (16, 32.0),
    (20, 38.0),
)


@dataclass(frozen=True, slots=True)
class MockInferenceSettings:
    """Timing and output model for one logical GPU execution stream.

    ``latency_ms`` and ``jitter_ms`` retain the old constant-delay API for
    deterministic tests. When latency is ``None``, the stereo batch curve and
    proportional jitter are used.
    """

    latency_ms: float | None = None
    jitter_ms: float | None = None
    worker_count: int = 1
    queue_capacity: int = 64
    seed: int = 7
    categories: tuple[str, ...] = ("acceptable", "insect_damage", "mould", "broken")
    weights: tuple[float, ...] = (0.65, 0.15, 0.10, 0.10)
    confidence_min: float = 0.70
    confidence_max: float = 0.99
    views_per_bean: int = 2
    max_batch_beans: int = 10
    result_deadline_ms: float = 60.0
    latency_curve: tuple[tuple[int, float], ...] = DEFAULT_STEREO_LATENCY_CURVE
    jitter_fraction: float = 0.15
    tail_probability: float = 0.01
    tail_latency_min_ms: float = 15.0
    tail_latency_max_ms: float = 30.0

    def validate(self) -> None:
        if self.latency_ms is not None and (
            not math.isfinite(self.latency_ms) or self.latency_ms < 0
        ):
            raise ValueError("mock inference latency must be finite and non-negative")
        if self.jitter_ms is not None and (
            not math.isfinite(self.jitter_ms) or self.jitter_ms < 0
        ):
            raise ValueError("mock inference jitter must be finite and non-negative")
        if self.latency_ms is None and self.jitter_ms is not None:
            raise ValueError("absolute jitter requires a constant latency override")
        if self.worker_count != 1:
            raise ValueError("the mock models one GPU and therefore requires one worker")
        if self.queue_capacity <= 0:
            raise ValueError("mock queue capacity must be positive")
        if not 1 <= self.max_batch_beans <= 16:
            raise ValueError("maximum batch beans must be between one and 16")
        if self.views_per_bean <= 0:
            raise ValueError("logical views per bean must be positive")
        for value, name in (
            (self.result_deadline_ms, "result deadline"),
            (self.tail_latency_min_ms, "minimum tail latency"),
            (self.tail_latency_max_ms, "maximum tail latency"),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"mock {name} must be finite and non-negative")
        if self.result_deadline_ms <= 0:
            raise ValueError("mock result deadline must be positive")
        if self.tail_latency_min_ms > self.tail_latency_max_ms:
            raise ValueError("mock tail latency minimum cannot exceed its maximum")
        if not math.isfinite(self.jitter_fraction) or not 0 <= self.jitter_fraction <= 1:
            raise ValueError("mock proportional jitter must be between zero and one")
        if not math.isfinite(self.tail_probability) or not 0 <= self.tail_probability <= 1:
            raise ValueError("mock tail probability must be between zero and one")
        if not self.latency_curve:
            raise ValueError("mock batch latency curve cannot be empty")
        previous_images = 0
        for image_count, latency in self.latency_curve:
            if image_count <= previous_images:
                raise ValueError("mock batch latency image counts must increase")
            if not math.isfinite(latency) or latency < 0:
                raise ValueError("mock batch latency values must be non-negative")
            previous_images = image_count
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

    def nominal_batch_latency_ms(self, bean_count: int) -> float:
        """Return interpolated model latency for a batch of logical bean pairs."""
        if bean_count <= 0:
            raise ValueError("batch bean count must be positive")
        if self.latency_ms is not None:
            return self.latency_ms
        image_count = bean_count * self.views_per_bean
        first_images, first_latency = self.latency_curve[0]
        if image_count <= first_images:
            return first_latency * image_count / first_images
        for (lower_images, lower_latency), (upper_images, upper_latency) in zip(
            self.latency_curve, self.latency_curve[1:]
        ):
            if image_count <= upper_images:
                fraction = (image_count - lower_images) / (
                    upper_images - lower_images
                )
                return lower_latency + fraction * (upper_latency - lower_latency)
        last_images, last_latency = self.latency_curve[-1]
        if len(self.latency_curve) == 1:
            return last_latency * image_count / last_images
        prior_images, prior_latency = self.latency_curve[-2]
        slope = (last_latency - prior_latency) / (last_images - prior_images)
        return last_latency + (image_count - last_images) * slope

@dataclass(frozen=True, slots=True)
class MockInferenceActivity:
    kind: str
    job_id: str
    bean_id: str
    category: str = ""
    confidence: float | None = None
    detail: str = ""
    crop: object | None = None
    batch_id: str = ""
    batch_beans: int = 0
    batch_images: int = 0
    batch_latency_ms: float | None = None
    queue_ms: float | None = None
    service_latency_ms: float | None = None
    tail_latency: bool = False
    deadline_missed: bool = False


@dataclass(frozen=True, slots=True)
class _QueuedInference:
    payload: CropPayload
    accepted_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class _QueuedBatch:
    items: tuple[_QueuedInference, ...]
    batch_id: str


class MockInferencerService:
    """Model each explicit source-frame crop group as one stereo GPU batch."""

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
        self._queue: queue.Queue[_QueuedBatch] = queue.Queue(
            maxsize=self.settings.queue_capacity
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stats_lock = threading.Lock()
        self._queued_beans = 0
        self._total_batch_beans = 0
        self._total_queue_ms = 0.0
        self._total_service_ms = 0.0
        self.ready = threading.Event()
        self.received = 0
        self.completed = 0
        self.dropped = 0
        self.batches = 0
        self.tail_batches = 0
        self.deadline_misses = 0
        self.last_batch_size = 0
        self.max_batch_size = 0
        self.last_batch_latency_ms = 0.0
        self.startup_error = ""

    def start(self) -> None:
        if self._threads:
            return
        receiver = threading.Thread(
            target=self._receive_loop,
            name="beano-mock-inferencer-receiver",
            daemon=True,
        )
        worker = threading.Thread(
            target=self._worker_loop,
            name="beano-mock-inferencer-gpu",
            daemon=True,
        )
        self._threads.extend((receiver, worker))
        receiver.start()
        worker.start()

    def close(self, *, drain: bool = True) -> None:
        if drain:
            self._queue.join()
        self._stop.set()
        for thread in self._threads:
            thread.join(2.0)
        self._threads.clear()

    def statistics(self) -> dict[str, int | float]:
        with self._stats_lock:
            batches = self.batches
            return {
                "received": self.received,
                "completed": self.completed,
                "dropped": self.dropped,
                "queued": self._queued_beans,
                "queued_batches": self._queue.qsize(),
                "batches": batches,
                "tail_batches": self.tail_batches,
                "deadline_misses": self.deadline_misses,
                "last_batch_size": self.last_batch_size,
                "max_batch_size": self.max_batch_size,
                "last_batch_latency_ms": self.last_batch_latency_ms,
                "mean_batch_size": (
                    self._total_batch_beans / batches if batches else 0.0
                ),
                "mean_queue_ms": (
                    self._total_queue_ms / self._total_batch_beans
                    if self._total_batch_beans
                    else 0.0
                ),
                "mean_service_ms": (
                    self._total_service_ms / self._total_batch_beans
                    if self._total_batch_beans
                    else 0.0
                ),
            }

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
                receiver.receive_batch(timeout_ms=100, accept=self._accept_batch)
        finally:
            receiver.close()

    def _accept_payload(self, payload: CropPayload) -> bool:
        return self._accept_batch((payload,))

    def _accept_batch(self, payloads: tuple[CropPayload, ...]) -> bool:
        if not payloads or len(payloads) > self.settings.max_batch_beans:
            with self._stats_lock:
                self.dropped += len(payloads)
            for payload in payloads:
                self._emit(
                    "rejected",
                    payload,
                    detail="frame batch exceeds configured bean limit",
                )
            return False
        accepted_ns = time.monotonic_ns()
        queued = _QueuedBatch(
            tuple(_QueuedInference(payload, accepted_ns) for payload in payloads),
            _frame_batch_id(payloads),
        )
        try:
            self._queue.put_nowait(queued)
        except queue.Full:
            with self._stats_lock:
                self.dropped += len(payloads)
            for payload in payloads:
                self._emit("rejected", payload, detail="mock inference queue full")
            return False
        with self._stats_lock:
            self.received += len(payloads)
            self._queued_beans += len(payloads)
        for payload in payloads:
            self._emit("received", payload, crop=payload.image_bgr)
        return True

    def _worker_loop(self) -> None:
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    queued = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                with self._stats_lock:
                    self._queued_beans -= len(queued.items)
                try:
                    self._process_batch(queued.items, queued.batch_id, registry)
                finally:
                    self._queue.task_done()
        finally:
            registry.close()

    def _process_batch(
        self,
        batch: tuple[_QueuedInference, ...],
        batch_id: str,
        registry: ZeroMQRegistryClient,
    ) -> None:
        batch_started_ns = time.monotonic_ns()
        job_ids = tuple(_stable_batch_job_key(item.payload.job) for item in batch)
        batch_randomizer = _batch_randomizer(
            self.settings.seed,
            job_ids,
        )
        nominal_ms = self.settings.nominal_batch_latency_ms(len(batch))
        if self.settings.latency_ms is not None:
            jitter_ms = batch_randomizer.uniform(
                -(self.settings.jitter_ms or 0.0), self.settings.jitter_ms or 0.0
            )
        else:
            jitter_ms = nominal_ms * batch_randomizer.uniform(
                -self.settings.jitter_fraction, self.settings.jitter_fraction
            )
        tail = batch_randomizer.random() < self.settings.tail_probability
        tail_ms = (
            batch_randomizer.uniform(
                self.settings.tail_latency_min_ms,
                self.settings.tail_latency_max_ms,
            )
            if tail
            else 0.0
        )
        delay_ms = max(0.0, nominal_ms + jitter_ms + tail_ms)
        image_count = len(batch) * self.settings.views_per_bean
        queue_times_ms = tuple(
            (batch_started_ns - item.accepted_monotonic_ns) / 1_000_000.0
            for item in batch
        )
        with self._stats_lock:
            self.batches += 1
            self.tail_batches += int(tail)
            self.last_batch_size = len(batch)
            self.max_batch_size = max(self.max_batch_size, len(batch))
            self.last_batch_latency_ms = delay_ms
            self._total_batch_beans += len(batch)
            self._total_queue_ms += sum(queue_times_ms)
        self._emit(
            "batch",
            batch_id=batch_id,
            batch_beans=len(batch),
            batch_images=image_count,
            batch_latency_ms=delay_ms,
            tail_latency=tail,
            detail=(
                f"{len(batch)} bean pair(s) / {image_count} images · "
                f"{delay_ms:.1f} ms" + (" · tail" if tail else "")
            ),
        )
        if self._stop.wait(delay_ms / 1_000.0):
            for item in batch:
                self._mark_failed(item.payload, "mock inferencer stopped")
            return

        batch_ready_ns = time.monotonic_ns()
        service_times_ms = tuple(
            (batch_ready_ns - item.accepted_monotonic_ns) / 1_000_000.0
            for item in batch
        )
        deadline_misses = tuple(
            latency_ms > self.settings.result_deadline_ms
            for latency_ms in service_times_ms
        )
        with self._stats_lock:
            self._total_service_ms += sum(service_times_ms)
            self.deadline_misses += sum(deadline_misses)

        sessions = {}
        for item, queue_ms, service_ms, deadline_missed in zip(
            batch, queue_times_ms, service_times_ms, deadline_misses
        ):
            payload = item.payload
            try:
                randomizer = _job_randomizer(
                    self.settings.seed,
                    _stable_job_key(payload.job),
                )
                category = randomizer.choices(
                    self.settings.categories,
                    weights=self.settings.weights,
                    k=1,
                )[0]
                confidence = randomizer.uniform(
                    self.settings.confidence_min,
                    self.settings.confidence_max,
                )
                run_id = payload.job.bean_ref.run_id
                session = sessions.get(run_id)
                if session is None:
                    session = registry.get_session(run_id)
                    sessions[run_id] = session
                result_timestamp = max(
                    payload.job.capture_timestamp_ns,
                    session.monotonic_to_source_ns(time.monotonic_ns()),
                )
                enrichment = Enrichment(
                    source="mock-inferencer",
                    kind="classification",
                    value={
                        "category": category,
                        "job_id": payload.job.job_id,
                        "inference": {
                            "profile": "resnet18-stereo-fp16-conservative-v1",
                            "input_mode": "logical_stereo",
                            "transported_camera": payload.job.camera_id,
                            "transported_views": 1,
                            "stereo_pair_complete": False,
                            "logical_views": self.settings.views_per_bean,
                            "batch_id": batch_id,
                            "batch_beans": len(batch),
                            "batch_images": image_count,
                            "batch_latency_ms": delay_ms,
                            "queue_ms": queue_ms,
                            "service_latency_ms": service_ms,
                            "result_deadline_ms": self.settings.result_deadline_ms,
                            "deadline_missed": deadline_missed,
                            "tail_latency": tail,
                        },
                    },
                    timestamp_ns=result_timestamp,
                    version="mock-resnet18-stereo-v2",
                    result_id=payload.job.job_id,
                    confidence=confidence,
                )
                completion_request_ns = time.monotonic_ns()
                registry.complete_inference_job(
                    payload.job.bean_ref,
                    payload.job.job_id,
                    enrichment,
                    timing_marks_ns={
                        **payload.job.timing_marks_ns,
                        "inference_received_monotonic_ns": (
                            item.accepted_monotonic_ns
                        ),
                        "inference_started_monotonic_ns": batch_started_ns,
                        "inference_completed_monotonic_ns": batch_ready_ns,
                        "registry_classification_request_monotonic_ns": (
                            completion_request_ns
                        ),
                    },
                    event_id=f"complete:{payload.job.job_id}",
                )
                with self._stats_lock:
                    self.completed += 1
                self._emit(
                    "completed",
                    payload,
                    category=category,
                    confidence=confidence,
                    batch_id=batch_id,
                    batch_beans=len(batch),
                    batch_images=image_count,
                    batch_latency_ms=delay_ms,
                    queue_ms=queue_ms,
                    service_latency_ms=service_ms,
                    tail_latency=tail,
                    deadline_missed=deadline_missed,
                    detail=(
                        f"{batch_id} · queue {queue_ms:.1f} ms · "
                        f"service {service_ms:.1f} ms"
                        + (" · SLA MISS" if deadline_missed else "")
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - job failure is observable state
                with self._stats_lock:
                    self.dropped += 1
                self._mark_failed(payload, str(exc), registry=registry)

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
        batch_id: str = "",
        batch_beans: int = 0,
        batch_images: int = 0,
        batch_latency_ms: float | None = None,
        queue_ms: float | None = None,
        service_latency_ms: float | None = None,
        tail_latency: bool = False,
        deadline_missed: bool = False,
    ) -> None:
        if self.activity is None:
            return
        self.activity(
            MockInferenceActivity(
                kind=kind,
                job_id="" if payload is None else payload.job.job_id,
                bean_id="" if payload is None else str(payload.job.bean_ref),
                category=category,
                confidence=confidence,
                detail=detail,
                crop=crop,
                batch_id=batch_id,
                batch_beans=batch_beans,
                batch_images=batch_images,
                batch_latency_ms=batch_latency_ms,
                queue_ms=queue_ms,
                service_latency_ms=service_latency_ms,
                tail_latency=tail_latency,
                deadline_missed=deadline_missed,
            )
        )


def _job_randomizer(seed: int, job_id: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{job_id}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _batch_randomizer(seed: int, job_ids: tuple[str, ...]) -> random.Random:
    digest = hashlib.sha256(
        f"{seed}:batch:{'|'.join(job_ids)}".encode()
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _stable_batch_job_key(job: InferenceJob) -> str:
    return f"{job.camera_id}:{job.frame_index}:{job.bean_ref.sequence}"


def _stable_job_key(job: InferenceJob) -> str:
    return f"{job.camera_id}:{job.bean_ref.sequence}"


def _frame_batch_id(payloads: tuple[CropPayload, ...]) -> str:
    first = payloads[0].job
    return ":".join(
        (
            "frame",
            first.bean_ref.run_id,
            first.camera_id,
            str(first.frame_index),
        )
    )
