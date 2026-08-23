"""Bounded recorded-source replay shared by the GUI and system tests."""

from __future__ import annotations

import gc
import math
import queue
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from .analysis import AnalysisEngine
from .crop import BeanCropSelector, CropPayload
from .inference_transport import ZeroMQCropClient
from .models import FrameAnalysis
from .registry_models import InferenceStatus, RunSession, RunState
from .registry_zmq import ZeroMQRegistryClient
from .sorting_context_transport import (
    SortingContext,
    ZeroMQSortingContextPublisher,
)
from .source import ReplaySource, SourceError
from .telemetry import SystemTelemetrySampler, TimingAccumulator, summarize_samples


@dataclass(frozen=True, slots=True)
class ReplaySettings:
    target_fps: float = 60.0
    preview_enabled: bool = False
    prebuffer_frames: int = 60
    maximum_frames: int = 1_000
    crop_queue_capacity: int = 16
    drop_stale_frames: bool = True
    maximum_frame_age_ms: float = 30.0
    clock_start_lead_ms: float = 50.0
    maximum_clock_offset_ms: float = 2.0
    suppress_cyclic_gc: bool = True

    def validate(self) -> None:
        if not math.isfinite(self.target_fps) or self.target_fps < 0:
            raise ValueError("target FPS must be finite and non-negative")
        if not 0 <= self.prebuffer_frames <= 120:
            raise ValueError("prebuffer frames must be between zero and 120")
        if not 1 <= self.maximum_frames <= 1_000:
            raise ValueError("maximum replay frames must be between 1 and 1000")
        if self.crop_queue_capacity <= 0:
            raise ValueError("crop queue capacity must be positive")
        if (
            not math.isfinite(self.maximum_frame_age_ms)
            or self.maximum_frame_age_ms <= 0
        ):
            raise ValueError("maximum frame age must be finite and positive")
        if (
            not math.isfinite(self.clock_start_lead_ms)
            or self.clock_start_lead_ms <= 0
        ):
            raise ValueError("clock start lead must be finite and positive")
        if (
            not math.isfinite(self.maximum_clock_offset_ms)
            or self.maximum_clock_offset_ms <= 0
        ):
            raise ValueError("maximum clock offset must be finite and positive")


@dataclass(frozen=True, slots=True)
class ReplayProgress:
    frame_index: int
    frame_count: int
    source_timestamp_ns: int
    source_read_ms: float
    processing_ms: float
    achieved_fps: float
    missed_deadlines: int
    crops_submitted: int
    crops_dropped: int
    crop_ms: float = 0.0
    frame_work_ms: float = 0.0
    frames_skipped: int = 0
    frame_age_ms: float = 0.0
    source_timeline_fps: float = 0.0


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    run_id: str
    frames_processed: int
    elapsed_seconds: float
    achieved_fps: float
    mean_source_read_ms: float
    max_source_read_ms: float
    mean_processing_ms: float
    max_processing_ms: float
    prebuffered_frames: int
    prebuffer_seconds: float
    missed_deadlines: int
    crops_submitted: int
    crops_dropped: int
    stopped: bool
    frames_skipped: int = 0
    source_timeline_fps: float = 0.0
    mean_frame_age_ms: float = 0.0
    max_frame_age_ms: float = 0.0
    clock_start_offset_ms: float = 0.0
    clock_anchor_attempts: int = 0
    clock_anchor_misses: int = 0
    clock_synchronized: bool = False
    timings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _RunClockStart:
    session: RunSession
    start_perf_counter_ns: int
    metrics: dict[str, object]


class DecodedFrameBuffer:
    """Bounded sequential decoder that overlaps input work with analysis."""

    def __init__(
        self, source: ReplaySource, *, frame_count: int, capacity: int
    ) -> None:
        self.source = source
        self.frame_count = max(0, min(frame_count, source.metadata.frame_count))
        self.capacity = max(1, capacity)
        self._target = min(self.frame_count, self.capacity)
        self._queue: queue.Queue[tuple[int, object, float]] = queue.Queue(
            maxsize=self.capacity
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._next_index = 0
        self.last_load_ms = 0.0

    def prebuffer(
        self,
        cancellation: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> tuple[int, float]:
        if self._thread is not None:
            raise RuntimeError("decoded frame buffer has already been started")
        started = time.perf_counter()
        self._thread = threading.Thread(
            target=self._decode,
            name="beanoflight-frame-prefetch",
            daemon=True,
        )
        self._thread.start()
        reported = -1
        while not self._ready.wait(0.025):
            buffered = self._queue.qsize()
            if buffered != reported and on_progress is not None:
                on_progress(buffered, self._target)
                reported = buffered
            if cancellation.is_set():
                self._stop.set()
                break
        buffered = self._queue.qsize()
        if on_progress is not None and buffered != reported:
            on_progress(buffered, self._target)
        self._raise_error()
        return buffered, time.perf_counter() - started

    def frame(self, index: int):
        if index != self._next_index:
            raise SourceError(
                "decoded frame buffer requires sequential access; "
                f"expected {self._next_index}, received {index}"
            )
        while True:
            try:
                buffered_index, frame, load_ms = self._queue.get(timeout=0.05)
            except queue.Empty:
                self._raise_error()
                if self._finished.is_set():
                    raise SourceError(f"decoded frame {index + 1} is unavailable")
                continue
            if buffered_index != index:
                raise SourceError(
                    f"decoded frame buffer returned {buffered_index}, expected {index}"
                )
            self._next_index += 1
            self.last_load_ms = load_ms
            return frame

    def close(self) -> None:
        self._stop.set()
        self._ready.set()
        thread = self._thread
        if thread is not None:
            thread.join(2.0)
        self._thread = None
        while True:
            try:
                _index, frame, _load_ms = self._queue.get_nowait()
            except queue.Empty:
                break
            _release_frame(self.source, frame)

    def _decode(self) -> None:
        try:
            for index in range(self.frame_count):
                if self._stop.is_set():
                    break
                while self._queue.full() and not self._stop.wait(0.01):
                    pass
                if self._stop.is_set():
                    break
                load_started = time.perf_counter_ns()
                frame = self.source.frame(index)
                load_ms = (time.perf_counter_ns() - load_started) / 1_000_000.0
                while not self._stop.is_set():
                    try:
                        self._queue.put((index, frame, load_ms), timeout=0.05)
                        break
                    except queue.Full:
                        continue
                else:
                    _release_frame(self.source, frame)
                if self._queue.qsize() >= self._target:
                    self._ready.set()
        except Exception as exc:  # noqa: BLE001 - propagated on the replay thread
            self._error = exc
        finally:
            self._finished.set()
            self._ready.set()

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error


@dataclass(frozen=True, slots=True)
class _FrameDispatch:
    updates: tuple
    payloads: tuple[CropPayload, ...]
    enqueued_ns: int


@dataclass(frozen=True, slots=True)
class _RegistryDispatch:
    updates: tuple
    jobs: tuple
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class _RegistryJobFailure:
    jobs: tuple
    detail: str
    attempts: int = 0


class CropDispatcher:
    """Persist frame state and deliver urgent crops off the analysis thread."""

    def __init__(
        self,
        registry_endpoint: str,
        inference_endpoint: str,
        *,
        capacity: int = 16,
        timeout_ms: int = 1_000,
        delivery_result: Callable[[tuple[CropPayload, ...], bool], None]
        | None = None,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.inference_endpoint = inference_endpoint
        self.timeout_ms = timeout_ms
        self.capacity = max(1, capacity)
        self.delivery_result = delivery_result
        self._items: deque[_FrameDispatch] = deque()
        self._condition = threading.Condition()
        self._active = 0
        self._registry_queue: queue.Queue[
            _RegistryDispatch | _RegistryJobFailure
        ] = queue.Queue(maxsize=max(64, self.capacity * 8))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._registry_thread: threading.Thread | None = None
        self.submitted = 0
        self.dropped = 0
        self.delivery_failures = 0
        self.track_frames_dropped = 0
        self.registry_batches = 0
        self.dispatch_items_coalesced = 0
        self.maximum_dispatch_items_per_batch = 0
        self._timings = {
            name: TimingAccumulator()
            for name in (
                "queue_delay_ms",
                "registry_frame_ms",
                "registry_urgent_ms",
                "registry_deferred_ms",
                "materialize_ms",
                "materialize_wait_ms",
                "inference_send_ms",
            )
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="beanoflight-crop-dispatch", daemon=True
        )
        self._registry_thread = threading.Thread(
            target=self._registry_loop,
            name="beanoflight-registry-persist",
            daemon=True,
        )
        self._registry_thread.start()
        self._thread.start()

    def register_and_enqueue(
        self, payload: CropPayload, registry: ZeroMQRegistryClient
    ) -> bool:
        return self.register_and_enqueue_many((payload,), registry)

    def register_and_enqueue_many(
        self,
        payloads: tuple[CropPayload, ...],
        _registry: ZeroMQRegistryClient,
    ) -> bool:
        return self.enqueue_frame((), payloads)

    def enqueue_frame(self, updates: tuple, payloads: tuple[CropPayload, ...]) -> bool:
        """Queue a frame, preferring crop-bearing work over track-only history."""

        queued_monotonic_ns = time.monotonic_ns()
        payloads = tuple(
            payload.with_job(
                replace(
                    payload.job,
                    timing_marks_ns={
                        **payload.job.timing_marks_ns,
                        "crop_queued_monotonic_ns": queued_monotonic_ns,
                    },
                )
            )
            for payload in payloads
        )
        item = _FrameDispatch(updates, payloads, time.perf_counter_ns())
        with self._condition:
            if len(self._items) >= self.capacity:
                if not payloads:
                    self.track_frames_dropped += 1
                    return False
                stale_index = next(
                    (
                        index
                        for index, pending in enumerate(self._items)
                        if not pending.payloads
                    ),
                    None,
                )
                if stale_index is None:
                    self.dropped += len(payloads)
                    return False
                del self._items[stale_index]
                self.track_frames_dropped += 1
            self._items.append(item)
            self.submitted += len(payloads)
            self._condition.notify()
            return True

    def performance_metrics(self) -> dict[str, dict[str, float | int]]:
        return {
            **{name: timing.summary() for name, timing in self._timings.items()},
            "track_frames_dropped": self.track_frames_dropped,
            "delivery_failures": self.delivery_failures,
            "registry_batches": self.registry_batches,
            "dispatch_items_coalesced": self.dispatch_items_coalesced,
            "maximum_dispatch_items_per_batch": (
                self.maximum_dispatch_items_per_batch
            ),
        }

    def close(self, *, drain: bool = True) -> None:
        if drain:
            with self._condition:
                self._condition.wait_for(
                    lambda: not self._items and self._active == 0,
                    timeout=10.0,
                )
            self._registry_queue.join()
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(2.0)
        registry_thread = self._registry_thread
        if registry_thread is not None:
            registry_thread.join(2.0)
        self._thread = None
        self._registry_thread = None

    def _run(self) -> None:
        sender = ZeroMQCropClient(self.inference_endpoint, timeout_ms=self.timeout_ms)
        right_materializer = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="beano-camr-materialize",
        )
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._items or self._stop.is_set(), timeout=0.05
                    )
                    if not self._items:
                        if self._stop.is_set():
                            break
                        continue
                    items = self._take_dispatch_batch_locked()
                    self._active += len(items)
                payloads = tuple(
                    payload for item in items for payload in item.payloads
                )
                all_updates = tuple(
                    update for item in items for update in item.updates
                )
                registered_jobs = ()
                try:
                    now_ns = time.perf_counter_ns()
                    for item in items:
                        queue_delay_ms = (
                            now_ns - item.enqueued_ns
                        ) / 1_000_000.0
                        for _payload in item.payloads or (None,):
                            self._timings["queue_delay_ms"].add(queue_delay_ms)
                    self.dispatch_items_coalesced += max(0, len(items) - 1)
                    self.maximum_dispatch_items_per_batch = max(
                        self.maximum_dispatch_items_per_batch, len(items)
                    )

                    if not payloads:
                        if not self._queue_registry(
                            _RegistryDispatch(all_updates, ())
                        ):
                            self.track_frames_dropped += len(items)
                        continue

                    materialized_items, materialize_ms = _materialize_payloads(
                        payloads,
                        time.monotonic_ns(),
                        right_executor=right_materializer,
                    )
                    inference_send_ns = time.monotonic_ns()
                    materialized_items = tuple(
                        payload.with_job(
                            replace(
                                payload.job,
                                timing_marks_ns={
                                    **payload.job.timing_marks_ns,
                                    "inference_send_monotonic_ns": inference_send_ns,
                                },
                            )
                        )
                        for payload in materialized_items
                    )
                    registered_jobs = tuple(
                        (payload.job, payload.job.job_id)
                        for payload in materialized_items
                    )
                    # Reserve persistence capacity, then hand the crop to inference
                    # without waiting for SQLite or a Registry acknowledgement.
                    if not self._queue_registry(
                        _RegistryDispatch(all_updates, registered_jobs)
                    ):
                        raise RuntimeError("asynchronous Registry queue is full")
                    send_started = time.perf_counter_ns()
                    sender.submit_batch(materialized_items)
                    if self.delivery_result is not None:
                        self.delivery_result(payloads, True)
                    send_ms = (
                        time.perf_counter_ns() - send_started
                    ) / 1_000_000.0
                    per_payload = len(materialized_items)
                    for _payload in materialized_items:
                        self._timings["materialize_ms"].add(
                            materialize_ms / per_payload
                        )
                        self._timings["materialize_wait_ms"].add(0.0)
                        self._timings["inference_send_ms"].add(
                            send_ms / per_payload
                        )
                except Exception as exc:  # noqa: BLE001 - durable failure state
                    self.dropped += len(payloads)
                    self.delivery_failures += len(payloads)
                    if payloads and self.delivery_result is not None:
                        self.delivery_result(payloads, False)
                    if registered_jobs:
                        self._queue_registry(
                            _RegistryJobFailure(
                                tuple(job for job, _event_id in registered_jobs),
                                str(exc),
                            )
                        )
                finally:
                    with self._condition:
                        self._active -= len(items)
                        self._condition.notify_all()
        finally:
            right_materializer.shutdown(wait=True, cancel_futures=True)
            sender.close()

    def _queue_registry(
        self, item: _RegistryDispatch | _RegistryJobFailure
    ) -> bool:
        try:
            self._registry_queue.put_nowait(item)
        except queue.Full:
            return False
        return True

    def _registry_loop(self) -> None:
        registry = ZeroMQRegistryClient(
            self.registry_endpoint, timeout_ms=self.timeout_ms
        )
        try:
            while not self._stop.is_set() or not self._registry_queue.empty():
                try:
                    item = self._registry_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    attempts = item.attempts
                    while True:
                        try:
                            stage_started = time.perf_counter_ns()
                            if isinstance(item, _RegistryDispatch):
                                registry.update_frame_and_submit_jobs(
                                    item.updates,
                                    item.jobs,
                                )
                                elapsed_ms = (
                                    time.perf_counter_ns() - stage_started
                                ) / 1_000_000.0
                                self._timings["registry_frame_ms"].add(elapsed_ms)
                                self._timings[
                                    "registry_urgent_ms"
                                    if item.jobs
                                    else "registry_deferred_ms"
                                ].add(elapsed_ms)
                                self.registry_batches += 1
                            else:
                                for job in item.jobs:
                                    registry.update_inference_job(
                                        job.bean_ref,
                                        job.job_id,
                                        InferenceStatus.DROPPED,
                                        job.capture_timestamp_ns,
                                        detail=item.detail,
                                        event_id=f"drop:{job.job_id}",
                                    )
                            break
                        except Exception:  # noqa: BLE001 - bounded persistence retry
                            registry.close()
                            attempts += 1
                            if attempts > 5 or self._stop.is_set():
                                if isinstance(item, _RegistryDispatch) and item.jobs:
                                    self.dropped += len(item.jobs)
                                else:
                                    self.track_frames_dropped += 1
                                break
                            # Retry in place. Moving a failed item to the queue tail
                            # can let later track updates overtake it and violate each
                            # bean's monotonically increasing capture timestamps.
                            self._stop.wait(0.01 * attempts)
                            registry = ZeroMQRegistryClient(
                                self.registry_endpoint, timeout_ms=self.timeout_ms
                            )
                finally:
                    self._registry_queue.task_done()
        finally:
            registry.close()

    def _take_dispatch_batch_locked(self) -> tuple[_FrameDispatch, ...]:
        """Coalesce queued track history through the first urgent crop frame.

        Updates retain their original order, so per-bean timestamps remain
        monotonic. A crop at the head is never delayed to collect later work.
        """

        first = self._items.popleft()
        selected = [first]
        if first.payloads:
            return tuple(selected)
        while self._items:
            item = self._items.popleft()
            selected.append(item)
            if item.payloads:
                break
        return tuple(selected)


def _materialize_payloads(
    payloads: tuple[CropPayload, ...],
    dispatch_dequeued_ns: int,
    *,
    right_executor: Executor | None = None,
) -> tuple[tuple[CropPayload, ...], float]:
    started = time.perf_counter_ns()
    materialized = []
    for payload in payloads:
        ready = payload.materialized(right_executor)
        materialized.append(
            ready.with_job(
                replace(
                    ready.job,
                    timing_marks_ns={
                        **ready.job.timing_marks_ns,
                        "dispatch_dequeued_monotonic_ns": dispatch_dequeued_ns,
                        "crop_materialized_monotonic_ns": time.monotonic_ns(),
                    },
                )
            )
        )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    return tuple(materialized), elapsed_ms


def _estimated_crossing_source_ns(track, line_y_mm: float, gravity_mm_s2: float):
    """Return a provisional physical deadline even for a one-hit track."""

    if track is None:
        return None
    _x, y_mm, _vx, vy_mm_s = track.state
    distance_mm = line_y_mm - y_mm
    if distance_mm <= 0:
        return track.timestamp_ns
    if gravity_mm_s2 <= 0:
        if vy_mm_s <= 0:
            return None
        seconds = distance_mm / vy_mm_s
    else:
        discriminant = vy_mm_s * vy_mm_s + 2.0 * gravity_mm_s2 * distance_mm
        if discriminant < 0:
            return None
        seconds = (-vy_mm_s + math.sqrt(discriminant)) / gravity_mm_s2
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return track.timestamp_ns + round(seconds * 1_000_000_000)


class ReplayRunner:
    def __init__(
        self,
        source: ReplaySource,
        engine: AnalysisEngine,
        registry: ZeroMQRegistryClient,
        *,
        settings: ReplaySettings | None = None,
        crop_selector: BeanCropSelector | None = None,
        crop_dispatcher: CropDispatcher | None = None,
        sorting_context_endpoint: str = "",
        profile_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.source = source
        self.engine = engine
        self.registry = registry
        self.settings = settings or ReplaySettings()
        self.settings.validate()
        self.crop_selector = crop_selector
        self.crop_dispatcher = crop_dispatcher
        self.sorting_context_endpoint = sorting_context_endpoint
        self.profile_metadata = dict(profile_metadata or {})
        if self.crop_dispatcher is not None:
            # Replay persistence shares the crop worker so the frame clock never
            # waits for SQLite or ZeroMQ. Interactive review without a dispatcher
            # keeps AnalysisEngine's original synchronous Registry behaviour.
            self.engine.registry = None
            if self.crop_selector is not None:
                self.crop_dispatcher.delivery_result = (
                    lambda payloads, succeeded: (
                        self.crop_selector.delivery_succeeded(payloads)
                        if succeeded
                        else self.crop_selector.delivery_failed(payloads)
                    )
                )

    def run(
        self,
        *,
        stop: threading.Event | None = None,
        paused: threading.Event | None = None,
        on_preview: Callable[[object, FrameAnalysis], None] | None = None,
        on_progress: Callable[[ReplayProgress], None] | None = None,
        on_prebuffer: Callable[[int, int], None] | None = None,
    ) -> ReplaySummary:
        cancellation = stop or threading.Event()
        pause = paused or threading.Event()
        metadata = self.source.metadata
        frame_limit = min(metadata.frame_count, self.settings.maximum_frames)
        run_id = self.engine.tracker.run_id
        created_ns = time.time_ns()
        start_source_ns = self.source.timestamp_ns(0)
        session = RunSession(
            run_id=run_id,
            revision=0,
            state=RunState.CREATED,
            source_path=str(Path(self.source.path)),
            source_kind=self.source.source_kind,
            frame_count=frame_limit,
            source_fps=metadata.fps,
            target_fps=self.settings.target_fps,
            source_start_timestamp_ns=start_source_ns,
            clock_source_timestamp_ns=start_source_ns,
            # CREATED sessions are deliberately unarmed. The authoritative
            # mapping is persisted as a future barrier only after all replay
            # workers and buffers are ready.
            clock_monotonic_ns=0,
            preview_enabled=self.settings.preview_enabled,
            created_timestamp_ns=created_ns,
            updated_timestamp_ns=created_ns,
            settings={
                "crop_size_px": (
                    None
                    if self.crop_selector is None
                    else self.crop_selector.settings.size_px
                ),
                "camera_id": (
                    "CamL/CamR"
                    if self.crop_selector is not None
                    and self.crop_selector.statistics()["stereo_enabled"]
                    else "CamL"
                ),
                "maximum_frames": self.settings.maximum_frames,
                "prebuffer_frames": self.settings.prebuffer_frames,
                "drop_stale_frames": self.settings.drop_stale_frames,
                "maximum_frame_age_ms": self.settings.maximum_frame_age_ms,
                "crops_per_bean": (
                    None
                    if self.crop_selector is None
                    else self.crop_selector.settings.max_crops_per_bean
                ),
                "crop_policy": (
                    None
                    if self.crop_selector is None
                    else {
                        "require_complete_bean_bbox": True,
                        "defer_track_birth_until_complete_bean_crop": True,
                        "allow_padding": self.crop_selector.settings.allow_padding,
                        "adaptive_edge_resize": (
                            self.crop_selector.settings.adaptive_edge_resize
                        ),
                    }
                ),
                "source_pipeline": getattr(self.source, "pipeline_metadata", {}),
                "execution_profile": self.profile_metadata,
                "suppress_cyclic_gc": self.settings.suppress_cyclic_gc,
            },
        )
        session = self.registry.put_session(session, expected_revision=0)
        registry_hot_start = _reset_registry_metrics(self.registry)
        frame_buffer = None
        prebuffered_frames = 0
        prebuffer_seconds = 0.0
        started = 0.0
        playback_elapsed = 0.0
        next_deadline = 0.0
        frame_count = 0
        processing_total = 0.0
        processing_max = 0.0
        source_read_total = 0.0
        source_read_max = 0.0
        missed = 0
        frames_skipped = 0
        frame_age_total = 0.0
        frame_age_max = 0.0
        was_paused = False
        failure: Exception | None = None
        timing_samples: defaultdict[str, list[float]] = defaultdict(list)
        system_telemetry = SystemTelemetrySampler()
        system_metrics: dict[str, object] = {}
        registry_metrics: dict[str, object] = {}
        crop_dispatch_metrics: dict[str, object] = {}
        crop_selection_metrics: dict[str, object] = {}
        sorting_context_metrics: dict[str, object] = {"enabled": False}
        source_metrics: dict[str, object] = {}
        clock_metrics: dict[str, object] = {
            "synchronized": False,
            "anchor_attempts": 0,
            "anchor_misses": 0,
            "start_offset_ms": 0.0,
        }
        gc_was_enabled = False
        sorting_context_publisher = None
        try:
            if self.settings.prebuffer_frames > 0:
                frame_buffer = DecodedFrameBuffer(
                    self.source,
                    frame_count=frame_limit,
                    capacity=self.settings.prebuffer_frames,
                )
                prebuffered_frames, prebuffer_seconds = frame_buffer.prebuffer(
                    cancellation, on_progress=on_prebuffer
                )
            if self.crop_dispatcher is not None:
                self.crop_dispatcher.start()
            if self.sorting_context_endpoint:
                sorting_context_publisher = ZeroMQSortingContextPublisher(
                    self.sorting_context_endpoint
                )
            system_telemetry.start()
            # A bounded replay creates many short-lived Python objects but no
            # intentional reference cycles on the acquisition path. Collect
            # before arming the shared clock and suppress cyclic-GC pauses while
            # frames are time-critical; reference counting continues normally.
            # Always restore the interpreter's prior state in the outer finally.
            gc_was_enabled = gc.isenabled()
            if self.settings.suppress_cyclic_gc and gc_was_enabled:
                gc.collect()
                gc.disable()
            clock_start = self._arm_run_clock(
                session,
                start_source_ns=start_source_ns,
                cancellation=cancellation,
            )
            session = clock_start.session
            clock_metrics = clock_start.metrics
            if not clock_metrics["synchronized"]:
                raise RuntimeError(str(clock_metrics["failure"]))
            started = clock_start.start_perf_counter_ns / 1_000_000_000.0
            next_deadline = started
            index = 0
            while index < frame_limit:
                if cancellation.is_set():
                    break
                if pause.is_set():
                    session = self.registry.put_session(
                        replace(
                            session,
                            state=RunState.PAUSED,
                            clock_source_timestamp_ns=(
                                start_source_ns
                                if index == 0
                                else self.source.timestamp_ns(index - 1)
                            ),
                            clock_monotonic_ns=time.monotonic_ns(),
                            updated_timestamp_ns=time.time_ns(),
                        ),
                        expected_revision=session.revision,
                    )
                    was_paused = True
                    while pause.is_set() and not cancellation.wait(0.05):
                        pass
                    if cancellation.is_set():
                        break
                if was_paused:
                    current_source = self.source.timestamp_ns(index)
                    resume_clock = self._arm_run_clock(
                        session,
                        start_source_ns=current_source,
                        cancellation=cancellation,
                    )
                    session = resume_clock.session
                    if not resume_clock.metrics["synchronized"]:
                        raise RuntimeError(str(resume_clock.metrics["failure"]))
                    clock_metrics = resume_clock.metrics
                    next_deadline = (
                        resume_clock.start_perf_counter_ns / 1_000_000_000.0
                    )
                    was_paused = False
                if (
                    self.settings.target_fps > 0
                    and self.settings.drop_stale_frames
                ):
                    interval = 1.0 / self.settings.target_fps
                    age_seconds = max(0.0, time.perf_counter() - next_deadline)
                    maximum_age = self.settings.maximum_frame_age_ms / 1_000.0
                    if age_seconds > maximum_age:
                        skip_count = min(
                            frame_limit - index,
                            max(1, math.ceil((age_seconds - maximum_age) / interval)),
                        )
                        for skipped_index in range(index, index + skip_count):
                            if frame_buffer is not None:
                                skipped_frame = frame_buffer.frame(skipped_index)
                                _release_frame(self.source, skipped_frame)
                        frames_skipped += skip_count
                        index += skip_count
                        next_deadline += interval * skip_count
                        continue
                frame_age_ms = max(
                    0.0, (time.perf_counter() - next_deadline) * 1_000.0
                )
                frame_age_total += frame_age_ms
                frame_age_max = max(frame_age_max, frame_age_ms)
                timing_samples["frame_age_ms"].append(frame_age_ms)
                read_started = time.perf_counter_ns()
                frame_work_started = read_started
                frame = None
                try:
                    frame = (
                        self.source.frame(index)
                        if frame_buffer is None
                        else frame_buffer.frame(index)
                    )
                    source_read_ms = (
                        (time.perf_counter_ns() - read_started) / 1_000_000.0
                        if frame_buffer is None
                        else frame_buffer.last_load_ms
                    )
                    source_read_total += source_read_ms
                    source_read_max = max(source_read_max, source_read_ms)
                    timing_samples["source_read_ms"].append(source_read_ms)
                    source_timestamp = self.source.timestamp_ns(index)
                    selected_crops: tuple[CropPayload, ...] = ()
                    crop_select_ms = 0.0

                    def select_urgent(
                        tracked: FrameAnalysis, _frame=frame
                    ) -> None:
                        nonlocal selected_crops, crop_select_ms
                        if self.crop_selector is None:
                            return
                        crop_started_ns = time.perf_counter_ns()
                        selected_crops = self.crop_selector.select(
                            _frame,
                            tracked,
                            {track.bean_ref: 1 for track in tracked.tracks},
                        )
                        crop_select_ms = (
                            time.perf_counter_ns() - crop_started_ns
                        ) / 1_000_000.0

                    tracked_callback = (
                        select_urgent
                        if self.crop_dispatcher is not None
                        and self.crop_selector is not None
                        else None
                    )
                    analysis = (
                        self.engine.process(frame, index, source_timestamp)
                        if tracked_callback is None
                        else self.engine.process(
                            frame,
                            index,
                            source_timestamp,
                            on_tracked=tracked_callback,
                        )
                    )
                    if sorting_context_publisher is not None:
                        prediction_by_ref = {
                            prediction.bean_ref: prediction
                            for prediction in analysis.predictions
                        }
                        sorting_context_publisher.send_batch(
                            run_id=run_id,
                            frame_index=index,
                            source_fps=session.source_fps,
                            target_fps=session.target_fps,
                            clock_source_timestamp_ns=(
                                session.clock_source_timestamp_ns
                            ),
                            clock_monotonic_ns=session.clock_monotonic_ns,
                            clock_epoch=session.revision,
                            items=tuple(
                                SortingContext(
                                    track,
                                    prediction_by_ref.get(track.bean_ref),
                                )
                                for track in analysis.tracks
                            ),
                        )
                    frame_count += 1
                    processing_total += analysis.processing_ms
                    processing_max = max(processing_max, analysis.processing_ms)
                    timing_samples["analysis_total_ms"].append(analysis.processing_ms)
                    if analysis.timings is not None:
                        for name, value in analysis.timings.as_dict().items():
                            timing_samples[name].append(value)
                    crop_started = time.perf_counter_ns()
                    if self.crop_dispatcher is not None:
                        prediction_by_ref = {
                            prediction.bean_ref: prediction
                            for prediction in analysis.predictions
                        }
                        track_by_ref = {
                            track.bean_ref: track for track in analysis.tracks
                        }
                        prioritized_crops = []
                        for payload in selected_crops:
                            prediction = prediction_by_ref.get(payload.job.bean_ref)
                            crossing_source_ns = (
                                prediction.crossing_timestamp_ns
                                if prediction is not None
                                else _estimated_crossing_source_ns(
                                    track_by_ref.get(payload.job.bean_ref),
                                    self.engine.gate_layout.line_y_mm,
                                    self.engine.tracker_settings.gravity_mm_s2,
                                )
                            )
                            timing_marks = {
                                **payload.job.timing_marks_ns,
                                "run_clock_source_ns": (
                                    session.clock_source_timestamp_ns
                                ),
                                "run_clock_monotonic_ns": session.clock_monotonic_ns,
                                "run_clock_epoch": session.revision,
                                "run_clock_scale_ppb": round(
                                    session.playback_scale * 1_000_000_000
                                ),
                            }
                            if crossing_source_ns is not None:
                                timing_marks[
                                    "inference_priority_crossing_source_ns"
                                ] = crossing_source_ns
                            payload = payload.with_job(
                                replace(
                                    payload.job,
                                    timing_marks_ns=timing_marks,
                                )
                            )
                            prioritized_crops.append(payload)
                        selected_crops = tuple(prioritized_crops)
                        updates = tuple(
                            (
                                track,
                                prediction_by_ref.get(track.bean_ref),
                                ":".join(
                                    (
                                        "track",
                                        track.bean_ref.run_id,
                                        str(track.bean_ref.sequence),
                                        str(index),
                                        track.status.value,
                                    )
                                ),
                            )
                            for track in analysis.tracks
                        )
                        accepted = self.crop_dispatcher.enqueue_frame(
                            updates, selected_crops
                        )
                        if not accepted and selected_crops:
                            self.crop_selector.delivery_failed(selected_crops)
                    crop_ms = (time.perf_counter_ns() - crop_started) / 1_000_000.0
                    timing_samples["crop_select_ms"].append(crop_select_ms)
                    timing_samples["frame_dispatch_enqueue_ms"].append(crop_ms)
                    if self.settings.preview_enabled and on_preview is not None:
                        on_preview(_preview_frame(self.source, frame), analysis)
                    frame_work_ms = (
                        time.perf_counter_ns() - frame_work_started
                    ) / 1_000_000.0
                    timing_samples["frame_work_ms"].append(frame_work_ms)
                finally:
                    if frame is not None:
                        _release_frame(self.source, frame)
                elapsed = max(time.perf_counter() - started, 1e-9)
                if on_progress is not None:
                    on_progress(
                        ReplayProgress(
                            index,
                            frame_limit,
                            source_timestamp,
                            source_read_ms,
                            analysis.processing_ms,
                            frame_count / elapsed,
                            missed,
                            0
                            if self.crop_dispatcher is None
                            else self.crop_dispatcher.submitted,
                            0
                            if self.crop_dispatcher is None
                            else self.crop_dispatcher.dropped,
                            crop_ms,
                            frame_work_ms,
                            frames_skipped,
                            frame_age_ms,
                            (frame_count + frames_skipped) / elapsed,
                        )
                    )
                if self.settings.target_fps > 0:
                    next_deadline += 1.0 / self.settings.target_fps
                    remaining = next_deadline - time.perf_counter()
                    if remaining > 0:
                        cancellation.wait(remaining)
                    else:
                        missed += 1
                index += 1
            playback_elapsed = max(time.perf_counter() - started, 1e-9)
        except Exception as exc:
            failure = exc
            if started > 0:
                playback_elapsed = max(time.perf_counter() - started, 1e-9)
            raise
        finally:
            if (
                self.settings.suppress_cyclic_gc
                and gc_was_enabled
                and not gc.isenabled()
            ):
                gc.enable()
            if frame_buffer is not None:
                frame_buffer.close()
            if self.crop_dispatcher is not None:
                self.crop_dispatcher.close(drain=True)
                crop_dispatch_metrics = self.crop_dispatcher.performance_metrics()
            if self.crop_selector is not None:
                crop_selection_metrics = self.crop_selector.statistics()
            source_statistics = getattr(self.source, "stereo_statistics", None)
            if callable(source_statistics):
                source_metrics = source_statistics()
            if sorting_context_publisher is not None:
                sorting_context_metrics = {
                    "enabled": True,
                    **sorting_context_publisher.statistics(),
                }
                sorting_context_publisher.close()
            system_metrics = system_telemetry.stop()
            registry_metrics = _registry_service_metrics(self.registry)
            final_state = RunState.FAILED if failure is not None else RunState.COMPLETED
            achieved_fps = (
                frame_count / playback_elapsed if playback_elapsed > 0 else 0.0
            )
            source_mean_ms = source_read_total / frame_count if frame_count else 0.0
            processing_mean_ms = processing_total / frame_count if frame_count else 0.0
            source_timeline_fps = (
                (frame_count + frames_skipped) / playback_elapsed
                if playback_elapsed > 0
                else 0.0
            )
            mean_frame_age_ms = frame_age_total / frame_count if frame_count else 0.0
            crops_submitted = (
                0 if self.crop_dispatcher is None else self.crop_dispatcher.submitted
            )
            crops_dropped = (
                0 if self.crop_dispatcher is None else self.crop_dispatcher.dropped
            )
            timing_summary = {
                name: summarize_samples(values)
                for name, values in sorted(timing_samples.items())
            }
            session = self.registry.put_session(
                replace(
                    session,
                    state=final_state,
                    updated_timestamp_ns=time.time_ns(),
                    settings={
                        **session.settings,
                        "frames_processed": frame_count,
                        "stopped": cancellation.is_set(),
                        "failure": "" if failure is None else str(failure),
                        "performance": {
                            "elapsed_seconds": playback_elapsed,
                            "achieved_fps": achieved_fps,
                            "source_timeline_fps": source_timeline_fps,
                            "mean_source_read_ms": source_mean_ms,
                            "max_source_read_ms": source_read_max,
                            "mean_processing_ms": processing_mean_ms,
                            "max_processing_ms": processing_max,
                            "prebuffered_frames": prebuffered_frames,
                            "prebuffer_seconds": prebuffer_seconds,
                            "missed_deadlines": missed,
                            "frames_skipped": frames_skipped,
                            "mean_frame_age_ms": mean_frame_age_ms,
                            "max_frame_age_ms": frame_age_max,
                            "crops_submitted": crops_submitted,
                            "crops_dropped": crops_dropped,
                            "timings_ms": timing_summary,
                            "registry": {
                                "hot_start": registry_hot_start,
                                "service": registry_metrics,
                            },
                            "system": system_metrics,
                            "crop_dispatch": crop_dispatch_metrics,
                            "crop_selection": crop_selection_metrics,
                            "source": source_metrics,
                            "sorting_context": sorting_context_metrics,
                            "clock": clock_metrics,
                        },
                    },
                ),
                expected_revision=session.revision,
            )
            evict_completed = getattr(self.registry, "evict_completed", None)
            if evict_completed is not None:
                evict_completed(
                    before_timestamp_ns=(1 << 63) - 1,
                    run_id=run_id,
                )
        return ReplaySummary(
            run_id=run_id,
            frames_processed=frame_count,
            elapsed_seconds=playback_elapsed,
            achieved_fps=achieved_fps,
            mean_source_read_ms=source_mean_ms,
            max_source_read_ms=source_read_max,
            mean_processing_ms=processing_mean_ms,
            max_processing_ms=processing_max,
            prebuffered_frames=prebuffered_frames,
            prebuffer_seconds=prebuffer_seconds,
            missed_deadlines=missed,
            crops_submitted=crops_submitted,
            crops_dropped=crops_dropped,
            stopped=cancellation.is_set(),
            frames_skipped=frames_skipped,
            source_timeline_fps=source_timeline_fps,
            mean_frame_age_ms=mean_frame_age_ms,
            max_frame_age_ms=frame_age_max,
            clock_start_offset_ms=float(clock_metrics["start_offset_ms"]),
            clock_anchor_attempts=int(clock_metrics["anchor_attempts"]),
            clock_anchor_misses=int(clock_metrics["anchor_misses"]),
            clock_synchronized=bool(clock_metrics["synchronized"]),
            timings={
                "timings_ms": timing_summary,
                "registry": {
                    "hot_start": registry_hot_start,
                    "service": registry_metrics,
                },
                "system": system_metrics,
                "crop_dispatch": crop_dispatch_metrics,
                "crop_selection": crop_selection_metrics,
                "source": source_metrics,
                "sorting_context": sorting_context_metrics,
                "clock": clock_metrics,
            },
        )

    def _arm_run_clock(
        self,
        session: RunSession,
        *,
        start_source_ns: int,
        cancellation: threading.Event,
    ) -> _RunClockStart:
        """Persist and meet a future clock barrier before releasing frame zero."""

        lead_ns = round(self.settings.clock_start_lead_ms * 1_000_000)
        minimum_margin_ns = min(5_000_000, max(500_000, lead_ns // 4))
        maximum_offset_ns = round(
            self.settings.maximum_clock_offset_ms * 1_000_000
        )
        attempts: list[dict[str, object]] = []
        misses = 0
        maximum_attempts = 4
        current = session
        for attempt in range(1, maximum_attempts + 1):
            anchor_monotonic_ns = time.monotonic_ns() + lead_ns
            expected_epoch = current.revision + 1
            clock_contract = {
                "version": "future-barrier-v1",
                "epoch": expected_epoch,
                "source_timestamp_ns": start_source_ns,
                "monotonic_ns": anchor_monotonic_ns,
                "playback_scale_ppb": round(
                    current.playback_scale * 1_000_000_000
                ),
            }
            previous_contracts = current.settings.get("clock_contracts", ())
            if not isinstance(previous_contracts, (list, tuple)):
                previous_contracts = ()
            update_started_ns = time.perf_counter_ns()
            current = self.registry.put_session(
                replace(
                    current,
                    state=RunState.RUNNING,
                    clock_source_timestamp_ns=start_source_ns,
                    clock_monotonic_ns=anchor_monotonic_ns,
                    updated_timestamp_ns=time.time_ns(),
                    settings={
                        **current.settings,
                        "clock_contract": clock_contract,
                        "clock_contracts": [
                            *previous_contracts,
                            clock_contract,
                        ],
                    },
                ),
                expected_revision=current.revision,
            )
            persisted_monotonic_ns = time.monotonic_ns()
            update_ms = (
                time.perf_counter_ns() - update_started_ns
            ) / 1_000_000.0
            margin_ns = anchor_monotonic_ns - persisted_monotonic_ns
            attempt_metrics: dict[str, object] = {
                "attempt": attempt,
                "epoch": current.revision,
                "registry_update_ms": update_ms,
                "post_persistence_margin_ms": margin_ns / 1_000_000.0,
            }
            if current.revision != expected_epoch or margin_ns < minimum_margin_ns:
                attempt_metrics["result"] = "persistence-missed-barrier"
                attempts.append(attempt_metrics)
                misses += 1
                continue
            if cancellation.wait(margin_ns / 1_000_000_000.0):
                attempt_metrics["result"] = "cancelled"
                attempts.append(attempt_metrics)
                return _RunClockStart(
                    current,
                    time.perf_counter_ns(),
                    {
                        "synchronized": False,
                        "failure": "replay cancelled while arming the run clock",
                        "start_offset_ms": 0.0,
                        "anchor_attempts": attempt,
                        "anchor_misses": misses,
                        "attempts": attempts,
                    },
                )
            actual_monotonic_ns = time.monotonic_ns()
            start_perf_counter_ns = time.perf_counter_ns()
            offset_ns = actual_monotonic_ns - anchor_monotonic_ns
            attempt_metrics["start_offset_ms"] = offset_ns / 1_000_000.0
            if abs(offset_ns) > maximum_offset_ns:
                attempt_metrics["result"] = "scheduler-missed-barrier"
                attempts.append(attempt_metrics)
                misses += 1
                continue
            attempt_metrics["result"] = "synchronized"
            attempts.append(attempt_metrics)
            return _RunClockStart(
                current,
                start_perf_counter_ns,
                {
                    "contract": "future-barrier-v1",
                    "epoch": current.revision,
                    "anchor_source_ns": start_source_ns,
                    "anchor_monotonic_ns": anchor_monotonic_ns,
                    "actual_start_monotonic_ns": actual_monotonic_ns,
                    "start_lead_ms": self.settings.clock_start_lead_ms,
                    "minimum_post_persistence_margin_ms": (
                        minimum_margin_ns / 1_000_000.0
                    ),
                    "maximum_offset_ms": self.settings.maximum_clock_offset_ms,
                    "start_offset_ms": offset_ns / 1_000_000.0,
                    "anchor_attempts": attempt,
                    "anchor_misses": misses,
                    "synchronized": True,
                    "attempts": attempts,
                },
            )
        return _RunClockStart(
            current,
            time.perf_counter_ns(),
            {
                "synchronized": False,
                "failure": (
                    "could not establish a synchronized replay clock after "
                    f"{maximum_attempts} attempts"
                ),
                "start_offset_ms": 0.0,
                "anchor_attempts": maximum_attempts,
                "anchor_misses": misses,
                "attempts": attempts,
            },
        )


def _release_frame(source: ReplaySource, frame: object) -> None:
    release = getattr(source, "release_frame", None)
    if release is not None:
        release(frame)


def _reset_registry_metrics(registry) -> dict[str, object]:
    reset = getattr(registry, "reset_service_metrics", None)
    if reset is None:
        return {"available": False}
    try:
        return {"available": True, **reset()}
    except Exception as exc:  # noqa: BLE001 - profiling must not stop replay
        return {"available": False, "error": str(exc)}


def _registry_service_metrics(registry) -> dict[str, object]:
    metrics = getattr(registry, "service_metrics", None)
    if metrics is None:
        return {"available": False}
    try:
        return {"available": True, **metrics()}
    except Exception as exc:  # noqa: BLE001 - profiling must not stop replay
        return {"available": False, "error": str(exc)}


def _preview_frame(source: ReplaySource, frame: object):
    convert = getattr(source, "preview_frame", None)
    return frame if convert is None else convert(frame)
