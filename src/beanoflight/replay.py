"""Bounded recorded-source replay shared by the GUI and system tests."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .analysis import AnalysisEngine
from .crop import BeanCropSelector, CropPayload
from .inference_transport import ZeroMQCropClient
from .models import FrameAnalysis
from .registry_models import InferenceStatus, RunSession, RunState
from .registry_zmq import RegistryRemoteError, ZeroMQRegistryClient
from .source import ReplaySource, SourceError
from .telemetry import SystemTelemetrySampler, TimingAccumulator, summarize_samples


@dataclass(frozen=True, slots=True)
class ReplaySettings:
    target_fps: float = 60.0
    preview_enabled: bool = False
    prebuffer_frames: int = 60
    maximum_frames: int = 1_000
    crop_queue_capacity: int = 16

    def validate(self) -> None:
        if not math.isfinite(self.target_fps) or self.target_fps < 0:
            raise ValueError("target FPS must be finite and non-negative")
        if not 0 <= self.prebuffer_frames <= 120:
            raise ValueError("prebuffer frames must be between zero and 120")
        if not 1 <= self.maximum_frames <= 1_000:
            raise ValueError("maximum replay frames must be between 1 and 1000")
        if self.crop_queue_capacity <= 0:
            raise ValueError("crop queue capacity must be positive")


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
    timings: dict[str, object] = field(default_factory=dict)


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


class CropDispatcher:
    """Bounded crop delivery isolated from the frame-processing thread."""

    def __init__(
        self,
        registry_endpoint: str,
        inference_endpoint: str,
        *,
        capacity: int = 16,
        timeout_ms: int = 1_000,
    ) -> None:
        self.registry_endpoint = registry_endpoint
        self.inference_endpoint = inference_endpoint
        self.timeout_ms = timeout_ms
        self._queue: queue.Queue[tuple[CropPayload, int]] = queue.Queue(
            maxsize=max(1, capacity)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.submitted = 0
        self.dropped = 0
        self._timings = {
            name: TimingAccumulator()
            for name in (
                "queue_delay_ms",
                "registry_submit_ms",
                "materialize_send_ms",
                "registry_accept_ms",
            )
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="beanoflight-crop-dispatch", daemon=True
        )
        self._thread.start()

    def register_and_enqueue(
        self, payload: CropPayload, registry: ZeroMQRegistryClient
    ) -> bool:
        stage_started = time.perf_counter_ns()
        compact_submit = getattr(registry, "submit_inference_job_revision", None)
        if compact_submit is None:
            registry.submit_inference_job(payload.job, event_id=payload.job.job_id)
        else:
            compact_submit(payload.job, event_id=payload.job.job_id)
        self._timings["registry_submit_ms"].add(
            (time.perf_counter_ns() - stage_started) / 1_000_000.0
        )
        try:
            self._queue.put_nowait((payload, time.perf_counter_ns()))
        except queue.Full:
            self.dropped += 1
            registry.update_inference_job(
                payload.job.bean_ref,
                payload.job.job_id,
                InferenceStatus.DROPPED,
                payload.job.capture_timestamp_ns,
                detail="crop dispatch queue full",
                event_id=f"drop:{payload.job.job_id}",
            )
            return False
        self.submitted += 1
        return True

    def performance_metrics(self) -> dict[str, dict[str, float | int]]:
        return {name: timing.summary() for name, timing in self._timings.items()}

    def close(self, *, drain: bool = True) -> None:
        if drain:
            self._queue.join()
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(2.0)
        self._thread = None

    def _run(self) -> None:
        registry = ZeroMQRegistryClient(
            self.registry_endpoint, timeout_ms=self.timeout_ms
        )
        sender = ZeroMQCropClient(self.inference_endpoint, timeout_ms=self.timeout_ms)
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    payload, enqueued_ns = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    self._timings["queue_delay_ms"].add(
                        (time.perf_counter_ns() - enqueued_ns) / 1_000_000.0
                    )
                    stage_started = time.perf_counter_ns()
                    sender.submit(payload.materialized())
                    self._timings["materialize_send_ms"].add(
                        (time.perf_counter_ns() - stage_started) / 1_000_000.0
                    )
                    try:
                        stage_started = time.perf_counter_ns()
                        registry.update_inference_job(
                            payload.job.bean_ref,
                            payload.job.job_id,
                            InferenceStatus.ACCEPTED,
                            payload.job.capture_timestamp_ns,
                            event_id=f"accept:{payload.job.job_id}",
                        )
                        self._timings["registry_accept_ms"].add(
                            (time.perf_counter_ns() - stage_started) / 1_000_000.0
                        )
                    except RegistryRemoteError:
                        # A zero-latency worker can complete the job between the
                        # crop ACK and this bookkeeping call. Completion is a
                        # successful terminal state, not a dispatch failure.
                        record = registry.get(
                            payload.job.bean_ref, include_history=False
                        )
                        current = next(
                            item
                            for item in record.inference_jobs
                            if item.job_id == payload.job.job_id
                        )
                        if current.status != InferenceStatus.COMPLETED:
                            raise
                except Exception as exc:  # noqa: BLE001 - failure is registry state
                    self.dropped += 1
                    try:
                        registry.update_inference_job(
                            payload.job.bean_ref,
                            payload.job.job_id,
                            InferenceStatus.DROPPED,
                            payload.job.capture_timestamp_ns,
                            detail=str(exc),
                            event_id=f"drop:{payload.job.job_id}",
                        )
                    except Exception:  # noqa: BLE001, S110 - both services may be down
                        pass
                finally:
                    self._queue.task_done()
        finally:
            sender.close()
            registry.close()


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
        profile_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.source = source
        self.engine = engine
        self.registry = registry
        self.settings = settings or ReplaySettings()
        self.settings.validate()
        self.crop_selector = crop_selector
        self.crop_dispatcher = crop_dispatcher
        self.profile_metadata = dict(profile_metadata or {})

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
            clock_monotonic_ns=time.monotonic_ns(),
            preview_enabled=self.settings.preview_enabled,
            created_timestamp_ns=created_ns,
            updated_timestamp_ns=created_ns,
            settings={
                "crop_size_px": (
                    None
                    if self.crop_selector is None
                    else self.crop_selector.settings.size_px
                ),
                "camera_id": "CamL",
                "maximum_frames": self.settings.maximum_frames,
                "prebuffer_frames": self.settings.prebuffer_frames,
                "crops_per_bean": (
                    None
                    if self.crop_selector is None
                    else self.crop_selector.settings.max_crops_per_bean
                ),
                "source_pipeline": getattr(self.source, "pipeline_metadata", {}),
                "execution_profile": self.profile_metadata,
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
        was_paused = False
        failure: Exception | None = None
        timing_samples: defaultdict[str, list[float]] = defaultdict(list)
        system_telemetry = SystemTelemetrySampler()
        system_metrics: dict[str, object] = {}
        registry_metrics: dict[str, object] = {}
        crop_dispatch_metrics: dict[str, object] = {}
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
            session = self.registry.put_session(
                replace(
                    session,
                    state=RunState.RUNNING,
                    clock_monotonic_ns=time.monotonic_ns(),
                    updated_timestamp_ns=time.time_ns(),
                ),
                expected_revision=session.revision,
            )
            if self.crop_dispatcher is not None:
                self.crop_dispatcher.start()
            system_telemetry.start()
            started = time.perf_counter()
            next_deadline = started
            for index in range(frame_limit):
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
                    session = self.registry.put_session(
                        replace(
                            session,
                            state=RunState.RUNNING,
                            clock_source_timestamp_ns=current_source,
                            clock_monotonic_ns=time.monotonic_ns(),
                            updated_timestamp_ns=time.time_ns(),
                        ),
                        expected_revision=session.revision,
                    )
                    next_deadline = time.perf_counter()
                    was_paused = False
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
                    analysis = self.engine.process(frame, index, source_timestamp)
                    frame_count += 1
                    processing_total += analysis.processing_ms
                    processing_max = max(processing_max, analysis.processing_ms)
                    timing_samples["analysis_total_ms"].append(analysis.processing_ms)
                    if analysis.timings is not None:
                        for name, value in analysis.timings.as_dict().items():
                            timing_samples[name].append(value)
                    crop_started = time.perf_counter_ns()
                    if (
                        self.crop_selector is not None
                        and self.crop_dispatcher is not None
                    ):
                        for crop in self.crop_selector.select(
                            frame, analysis, self.engine.last_registry_revisions
                        ):
                            self.crop_dispatcher.register_and_enqueue(
                                crop, self.registry
                            )
                    crop_ms = (time.perf_counter_ns() - crop_started) / 1_000_000.0
                    timing_samples["crop_select_register_ms"].append(crop_ms)
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
                        )
                    )
                if self.settings.target_fps > 0:
                    next_deadline += 1.0 / self.settings.target_fps
                    remaining = next_deadline - time.perf_counter()
                    if remaining > 0:
                        cancellation.wait(remaining)
                    else:
                        missed += 1
            playback_elapsed = max(time.perf_counter() - started, 1e-9)
        except Exception as exc:
            failure = exc
            if started > 0:
                playback_elapsed = max(time.perf_counter() - started, 1e-9)
            raise
        finally:
            if frame_buffer is not None:
                frame_buffer.close()
            if self.crop_dispatcher is not None:
                self.crop_dispatcher.close(drain=True)
                crop_dispatch_metrics = self.crop_dispatcher.performance_metrics()
            system_metrics = system_telemetry.stop()
            registry_metrics = _registry_service_metrics(self.registry)
            final_state = RunState.FAILED if failure is not None else RunState.COMPLETED
            achieved_fps = (
                frame_count / playback_elapsed if playback_elapsed > 0 else 0.0
            )
            source_mean_ms = source_read_total / frame_count if frame_count else 0.0
            processing_mean_ms = processing_total / frame_count if frame_count else 0.0
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
                            "mean_source_read_ms": source_mean_ms,
                            "max_source_read_ms": source_read_max,
                            "mean_processing_ms": processing_mean_ms,
                            "max_processing_ms": processing_max,
                            "prebuffered_frames": prebuffered_frames,
                            "prebuffer_seconds": prebuffer_seconds,
                            "missed_deadlines": missed,
                            "crops_submitted": crops_submitted,
                            "crops_dropped": crops_dropped,
                            "timings_ms": timing_summary,
                            "registry": {
                                "hot_start": registry_hot_start,
                                "service": registry_metrics,
                            },
                            "system": system_metrics,
                            "crop_dispatch": crop_dispatch_metrics,
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
            timings={
                "timings_ms": timing_summary,
                "registry": {
                    "hot_start": registry_hot_start,
                    "service": registry_metrics,
                },
                "system": system_metrics,
                "crop_dispatch": crop_dispatch_metrics,
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
