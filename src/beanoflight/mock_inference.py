"""Deterministic, batched classifier used to exercise asynchronous inference."""

from __future__ import annotations

import hashlib
import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .classification import CLASSIFICATION_EVIDENCE
from .classification_transport import (
    DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
    DirectInferenceEvidence,
    ZeroMQDirectEvidencePublisher,
)
from .crop import CropPayload
from .inference_transport import DEFAULT_CROP_ENDPOINT, ZeroMQCropReceiver
from .registry_models import Enrichment, InferenceJob, InferenceStatus
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import (
    CAPABILITY_COMPLETE_INFERENCE_JOBS_ACK,
    RegistryRemoteError,
    ZeroMQRegistryClient,
)

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


@dataclass(frozen=True, slots=True, order=True)
class _QueuedBatch:
    priority_deadline_ns: int
    arrival_order: int
    items: tuple[_QueuedInference, ...] = field(compare=False)
    batch_id: str = field(compare=False)


@dataclass(frozen=True, slots=True)
class _CompletedBatch:
    items: tuple[_QueuedInference, ...]
    batch_id: str
    batch_started_ns: int
    batch_ready_ns: int
    delay_ms: float
    image_count: int
    queue_times_ms: tuple[float, ...]
    service_times_ms: tuple[float, ...]
    deadline_misses: tuple[bool, ...]
    tail: bool
    failure: str = ""


class MockInferencerService:
    """Model each explicit source-frame crop group as one stereo GPU batch."""

    def __init__(
        self,
        *,
        registry_endpoint: str = DEFAULT_COMMAND_ENDPOINT,
        crop_endpoint: str = DEFAULT_CROP_ENDPOINT,
        classification_endpoint: str = DEFAULT_DIRECT_EVIDENCE_ENDPOINT,
        settings: MockInferenceSettings | None = None,
        activity: Callable[[MockInferenceActivity], None] | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.crop_endpoint = crop_endpoint
        self.classification_endpoint = classification_endpoint
        self.settings = settings or MockInferenceSettings()
        self.settings.validate()
        self.activity = activity
        self._queue: queue.PriorityQueue[_QueuedBatch] = queue.PriorityQueue(
            maxsize=self.settings.queue_capacity
        )
        self._results: queue.Queue[_CompletedBatch] = queue.Queue(
            maxsize=self.settings.queue_capacity
        )
        self._arrival_order = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stats_lock = threading.Lock()
        self._queued_beans = 0
        self._total_batch_beans = 0
        self._total_queue_ms = 0.0
        self._total_service_ms = 0.0
        self._sessions = {}
        self._registry_capabilities: frozenset[str] | None = None
        self.ready = threading.Event()
        self.received = 0
        self.completed = 0
        self.dropped = 0
        self.batches = 0
        self.tail_batches = 0
        self.deadline_misses = 0
        self.direct_batches_sent = 0
        self.direct_batches_dropped = 0
        self.direct_evidence_sent = 0
        self.direct_evidence_dropped = 0
        self.registry_completion_retries = 0
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
        publisher = threading.Thread(
            target=self._result_loop,
            name="beano-mock-inferencer-results",
            daemon=True,
        )
        self._threads.extend((receiver, worker, publisher))
        receiver.start()
        worker.start()
        publisher.start()

    def close(self, *, drain: bool = True) -> None:
        if drain:
            self._queue.join()
            self._results.join()
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
                "results_pending": self._results.qsize(),
                "batches": batches,
                "tail_batches": self.tail_batches,
                "deadline_misses": self.deadline_misses,
                "direct_batches_sent": self.direct_batches_sent,
                "direct_batches_dropped": self.direct_batches_dropped,
                "direct_evidence_sent": self.direct_evidence_sent,
                "direct_evidence_dropped": self.direct_evidence_dropped,
                "registry_completion_retries": self.registry_completion_retries,
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
        self._arrival_order += 1
        queued = _QueuedBatch(
            _batch_priority_deadline_ns(
                payloads,
                accepted_ns,
                self.settings.result_deadline_ms,
            ),
            self._arrival_order,
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
        while not self._stop.is_set() or not self._queue.empty():
            try:
                queued = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._stats_lock:
                self._queued_beans -= len(queued.items)
            try:
                completed = self._process_batch(queued.items, queued.batch_id)
                self._results.put(completed)
            finally:
                self._queue.task_done()

    def _result_loop(self) -> None:
        registry = ZeroMQRegistryClient(self.registry_endpoint, timeout_ms=2_000)
        direct = (
            ZeroMQDirectEvidencePublisher(self.classification_endpoint)
            if self.classification_endpoint
            else None
        )
        try:
            try:
                self._registry_capabilities = _registry_capabilities(registry)
            except Exception:  # noqa: BLE001 - retry on the first result batch
                self._registry_capabilities = None
            while not self._stop.is_set() or not self._results.empty():
                try:
                    completed = self._results.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    self._publish_completed_batch(completed, registry, direct)
                finally:
                    self._results.task_done()
        finally:
            if direct is not None:
                direct.close()
            registry.close()

    def _process_batch(
        self,
        batch: tuple[_QueuedInference, ...],
        batch_id: str,
    ) -> _CompletedBatch:
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
            stopped_ns = time.monotonic_ns()
            queue_times_ms = tuple(
                (batch_started_ns - item.accepted_monotonic_ns) / 1_000_000.0
                for item in batch
            )
            service_times_ms = tuple(
                (stopped_ns - item.accepted_monotonic_ns) / 1_000_000.0
                for item in batch
            )
            return _CompletedBatch(
                batch,
                batch_id,
                batch_started_ns,
                stopped_ns,
                delay_ms,
                image_count,
                queue_times_ms,
                service_times_ms,
                tuple(False for _item in batch),
                tail,
                "mock inferencer stopped",
            )

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

        return _CompletedBatch(
            batch,
            batch_id,
            batch_started_ns,
            batch_ready_ns,
            delay_ms,
            image_count,
            queue_times_ms,
            service_times_ms,
            deadline_misses,
            tail,
        )

    def _publish_completed_batch(
        self,
        completed: _CompletedBatch,
        registry: ZeroMQRegistryClient,
        direct: ZeroMQDirectEvidencePublisher | None = None,
    ) -> None:
        batch = completed.items
        batch_id = completed.batch_id
        batch_started_ns = completed.batch_started_ns
        batch_ready_ns = completed.batch_ready_ns
        delay_ms = completed.delay_ms
        image_count = completed.image_count
        queue_times_ms = completed.queue_times_ms
        service_times_ms = completed.service_times_ms
        deadline_misses = completed.deadline_misses
        tail = completed.tail
        if completed.failure:
            for item in batch:
                self._mark_failed(
                    item.payload,
                    completed.failure,
                    registry=registry,
                )
            return

        completions = []
        direct_items = []
        results = []
        completion_request_ns = time.monotonic_ns()
        for item, queue_ms, service_ms, deadline_missed in zip(
            batch, queue_times_ms, service_times_ms, deadline_misses
        ):
            payload = item.payload
            bean_randomizer = _job_randomizer(
                self.settings.seed,
                _stable_job_key(payload.job),
            )
            category = bean_randomizer.choices(
                self.settings.categories,
                weights=self.settings.weights,
                k=1,
            )[0]
            sample_index = _job_sample_index(payload.job)
            sample_randomizer = _job_randomizer(
                self.settings.seed,
                _stable_sample_job_key(payload.job, sample_index),
            )
            confidence = sample_randomizer.uniform(
                self.settings.confidence_min,
                self.settings.confidence_max,
            )
            probabilities = _mock_probability_vector(
                self.settings.categories,
                category,
                confidence,
                sample_randomizer,
            )
            confidence = probabilities[self.settings.categories.index(category)]
            logits = tuple(math.log(max(value, 1e-12)) for value in probabilities)
            run_id = payload.job.bean_ref.run_id
            marks = payload.job.timing_marks_ns
            expected_samples = int(marks.get("expected_inference_samples", 0))
            session = self._sessions.get(run_id)
            if expected_samples <= 0 or not _job_has_run_clock(payload.job):
                if session is None:
                    session = registry.get_session(run_id)
                    self._sessions[run_id] = session
                if expected_samples <= 0:
                    expected_samples = int(
                        getattr(session, "settings", {}).get("crops_per_bean")
                        or 1
                    )
            expected_samples = max(1, min(5, expected_samples))
            ensemble_id = (
                f"{payload.job.bean_ref.run_id}:"
                f"{payload.job.bean_ref.sequence}:mock-resnet18-stereo-v3"
            )
            result_timestamp = max(
                payload.job.capture_timestamp_ns,
                _job_monotonic_to_source_ns(
                    payload.job,
                    completion_request_ns,
                    fallback_session=session,
                ),
            )
            enrichment = Enrichment(
                source="mock-inferencer",
                kind=CLASSIFICATION_EVIDENCE,
                value={
                    "category": category,
                    "class_order": list(self.settings.categories),
                    "probabilities": list(probabilities),
                    "logits": list(logits),
                    "job_id": payload.job.job_id,
                    "ensemble": {
                        "id": ensemble_id,
                        "sample_index": sample_index,
                        "expected_samples": expected_samples,
                    },
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
                version="mock-resnet18-stereo-v3",
                result_id=payload.job.job_id,
                confidence=confidence,
            )
            timing_marks = {
                **payload.job.timing_marks_ns,
                "inference_received_monotonic_ns": item.accepted_monotonic_ns,
                "inference_started_monotonic_ns": batch_started_ns,
                "inference_completed_monotonic_ns": batch_ready_ns,
                "registry_classification_request_monotonic_ns": (
                    completion_request_ns
                ),
            }
            completions.append(
                (
                    payload.job.bean_ref,
                    payload.job.job_id,
                    enrichment,
                    timing_marks,
                    f"complete:{payload.job.job_id}",
                )
            )
            direct_items.append(DirectInferenceEvidence(payload.job, enrichment))
            results.append(
                (payload, category, confidence, queue_ms, service_ms, deadline_missed)
            )
        if direct is not None:
            direct_attempt_ns = time.monotonic_ns()
            try:
                sent, direct_sent_ns = direct.send_batch(
                    batch_id, tuple(direct_items)
                )
            except Exception as exc:  # noqa: BLE001 - Registry remains recovery path
                sent = False
                direct_sent_ns = time.monotonic_ns()
                self._emit("error", detail=f"direct evidence: {exc}")
            direct_completed_ns = time.monotonic_ns()
            for _bean_ref, _job_id, _enrichment, marks, _event_id in completions:
                marks.update(
                    {
                        "direct_delivery_attempted": 1,
                        "direct_delivery_acknowledged": int(sent),
                        "direct_delivery_attempt_monotonic_ns": direct_attempt_ns,
                        "direct_delivery_completed_monotonic_ns": (
                            direct_completed_ns
                        ),
                    }
                )
            with self._stats_lock:
                if sent:
                    self.direct_batches_sent += 1
                    self.direct_evidence_sent += len(direct_items)
                else:
                    self.direct_batches_dropped += 1
                    self.direct_evidence_dropped += len(direct_items)
            if sent:
                for _bean_ref, _job_id, _enrichment, marks, _event_id in completions:
                    marks["direct_result_send_monotonic_ns"] = direct_sent_ns
        try:
            if self._registry_capabilities is None:
                self._registry_capabilities = _registry_capabilities(registry)
            retries = _complete_inference_batch_with_registration_retry(
                registry,
                tuple(completions),
                self._registry_capabilities,
            )
            with self._stats_lock:
                self.completed += len(results)
                self.registry_completion_retries += retries
            for (
                payload,
                category,
                confidence,
                queue_ms,
                service_ms,
                deadline_missed,
            ) in results:
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
        except Exception as exc:  # noqa: BLE001 - batch failure is observable state
            with self._stats_lock:
                self.dropped += len(results)
            for payload, *_result in results:
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


def _registry_capabilities(registry) -> frozenset[str]:
    """Return advertised features; an older Registry advertises none."""

    ping = getattr(registry, "ping", None)
    if ping is None:
        return frozenset()
    response = ping()
    raw = (response.get("capabilities") or ()) if isinstance(response, dict) else ()
    return frozenset(str(item) for item in raw)


def _job_has_run_clock(job: InferenceJob) -> bool:
    marks = job.timing_marks_ns
    return (
        int(marks.get("run_clock_monotonic_ns", 0)) > 0
        and int(marks.get("run_clock_scale_ppb", 0)) > 0
        and "run_clock_source_ns" in marks
    )


def _job_monotonic_to_source_ns(
    job: InferenceJob,
    monotonic_ns: int,
    *,
    fallback_session=None,
) -> int:
    marks = job.timing_marks_ns
    if _job_has_run_clock(job):
        source_ns = int(marks["run_clock_source_ns"])
        clock_ns = int(marks["run_clock_monotonic_ns"])
        scale_ppb = int(marks["run_clock_scale_ppb"])
        return source_ns + round(
            (monotonic_ns - clock_ns) * scale_ppb / 1_000_000_000
        )
    if fallback_session is not None:
        return fallback_session.monotonic_to_source_ns(monotonic_ns)
    return job.capture_timestamp_ns


def _complete_inference_batch_with_registration_retry(
    registry,
    completions,
    capabilities,
    *,
    maximum_attempts: int = 8,
) -> int:
    """Allow a crop result to race its asynchronous job registration safely."""

    retries = 0
    while True:
        try:
            _complete_inference_batch(registry, completions, capabilities)
            return retries
        except Exception as exc:
            if (
                retries >= maximum_attempts
                or "inference job does not exist" not in str(exc).lower()
            ):
                raise
            # Registration normally trails crop delivery by only a few ms.  The
            # direct evidence has already reached the sorter; this bounded retry
            # is solely for durable audit completion and never delays actuation.
            time.sleep(min(0.025, 0.001 * (2**retries)))
            retries += 1


def _complete_inference_batch(registry, completions, capabilities) -> None:
    """Use the fastest remotely supported completion contract with safe fallback."""

    complete_ack = getattr(registry, "complete_inference_jobs_ack", None)
    if (
        CAPABILITY_COMPLETE_INFERENCE_JOBS_ACK in capabilities
        and complete_ack is not None
    ):
        try:
            complete_ack(completions)
            return
        except RegistryRemoteError as exc:
            if not _is_unknown_operation(exc, "complete_inference_jobs_ack"):
                raise

    complete_many = getattr(registry, "complete_inference_jobs", None)
    if complete_many is not None:
        try:
            complete_many(completions)
            return
        except RegistryRemoteError as exc:
            if not _is_unknown_operation(exc, "complete_inference_jobs"):
                raise

    for bean_ref, job_id, enrichment, timing_marks, event_id in completions:
        registry.complete_inference_job(
            bean_ref,
            job_id,
            enrichment,
            timing_marks_ns=timing_marks,
            event_id=event_id,
        )


def _is_unknown_operation(exc: RegistryRemoteError, operation: str) -> bool:
    return (
        exc.error_type == "ValueError"
        and f"unknown registry operation: {operation}" in exc.remote_message
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


def _stable_sample_job_key(job: InferenceJob, sample_index: int) -> str:
    return f"{_stable_job_key(job)}:sample:{sample_index}"


def _job_sample_index(job: InferenceJob) -> int:
    try:
        return int(job.job_id.rsplit(":", 1)[1]) + 1
    except (IndexError, ValueError):
        return 1


def _mock_probability_vector(
    categories: tuple[str, ...],
    category: str,
    confidence: float,
    randomizer: random.Random,
) -> tuple[float, ...]:
    """Return a complete, deterministic softmax-like mock output."""

    if len(categories) == 1:
        return (1.0,)
    winner = categories.index(category)
    remaining = max(0.0, 1.0 - confidence)
    shares = [
        0.05 + randomizer.random() if index != winner else 0.0
        for index in range(len(categories))
    ]
    share_total = sum(shares)
    probabilities = [remaining * share / share_total for share in shares]
    probabilities[winner] = confidence
    # Assign the floating point remainder to the winner so validation can use a
    # strict probability-sum tolerance after JSON round trips.
    probabilities[winner] += 1.0 - sum(probabilities)
    return tuple(probabilities)


def _batch_priority_deadline_ns(
    payloads: tuple[CropPayload, ...],
    accepted_monotonic_ns: int,
    fallback_deadline_ms: float,
) -> int:
    """Map source-clock crossing estimates onto a comparable local deadline.

    A batch remains one source-frame unit. This key only changes which waiting
    frame batch runs next when the simulated GPU is already occupied.
    """

    deadlines = []
    for payload in payloads:
        marks = payload.job.timing_marks_ns
        crossing_source_ns = marks.get(
            "inference_priority_crossing_source_ns",
            marks.get("predicted_crossing_source_ns"),
        )
        if crossing_source_ns is None:
            continue
        remaining_ns = max(
            0,
            int(crossing_source_ns) - payload.job.capture_timestamp_ns,
        )
        deadlines.append(accepted_monotonic_ns + remaining_ns)
    if deadlines:
        return min(deadlines)
    return accepted_monotonic_ns + round(fallback_deadline_ms * 1_000_000)


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
