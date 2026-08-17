"""Bounded recorded-source replay shared by the GUI and system tests."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .analysis import AnalysisEngine
from .crop import BeanCropSelector, CropPayload
from .inference_transport import ZeroMQCropClient
from .models import FrameAnalysis
from .registry_models import InferenceStatus, RunSession, RunState
from .registry_zmq import RegistryRemoteError, ZeroMQRegistryClient
from .source import ReplaySource, SourceError


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
        self._queue: queue.Queue[CropPayload] = queue.Queue(maxsize=max(1, capacity))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.submitted = 0
        self.dropped = 0

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
        registry.submit_inference_job(payload.job, event_id=payload.job.job_id)
        try:
            self._queue.put_nowait(payload)
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
                    payload = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    sender.submit(payload.materialized())
                    try:
                        registry.update_inference_job(
                            payload.job.bean_ref,
                            payload.job.job_id,
                            InferenceStatus.ACCEPTED,
                            payload.job.capture_timestamp_ns,
                            event_id=f"accept:{payload.job.job_id}",
                        )
                    except RegistryRemoteError:
                        # A zero-latency worker can complete the job between the
                        # crop ACK and this bookkeeping call. Completion is a
                        # successful terminal state, not a dispatch failure.
                        record = registry.get(payload.job.bean_ref)
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
    ) -> None:
        self.source = source
        self.engine = engine
        self.registry = registry
        self.settings = settings or ReplaySettings()
        self.settings.validate()
        self.crop_selector = crop_selector
        self.crop_dispatcher = crop_dispatcher

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
            },
        )
        session = self.registry.put_session(session, expected_revision=0)
        frame_buffer = None
        prebuffered_frames = 0
        prebuffer_seconds = 0.0
        started = 0.0
        next_deadline = 0.0
        frame_count = 0
        processing_total = 0.0
        processing_max = 0.0
        source_read_total = 0.0
        source_read_max = 0.0
        missed = 0
        was_paused = False
        failure: Exception | None = None
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
                    source_timestamp = self.source.timestamp_ns(index)
                    analysis = self.engine.process(frame, index, source_timestamp)
                    frame_count += 1
                    processing_total += analysis.processing_ms
                    processing_max = max(processing_max, analysis.processing_ms)
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
                    if self.settings.preview_enabled and on_preview is not None:
                        on_preview(_preview_frame(self.source, frame), analysis)
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
                        )
                    )
                if self.settings.target_fps > 0:
                    next_deadline += 1.0 / self.settings.target_fps
                    remaining = next_deadline - time.perf_counter()
                    if remaining > 0:
                        cancellation.wait(remaining)
                    else:
                        missed += 1
        except Exception as exc:
            failure = exc
            raise
        finally:
            if frame_buffer is not None:
                frame_buffer.close()
            if self.crop_dispatcher is not None:
                self.crop_dispatcher.close(drain=True)
            final_state = RunState.FAILED if failure is not None else RunState.COMPLETED
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
                    },
                ),
                expected_revision=session.revision,
            )
        elapsed = max(time.perf_counter() - started, 1e-9)
        return ReplaySummary(
            run_id=run_id,
            frames_processed=frame_count,
            elapsed_seconds=elapsed,
            achieved_fps=frame_count / elapsed,
            mean_source_read_ms=(
                source_read_total / frame_count if frame_count else 0.0
            ),
            max_source_read_ms=source_read_max,
            mean_processing_ms=(processing_total / frame_count if frame_count else 0.0),
            max_processing_ms=processing_max,
            prebuffered_frames=prebuffered_frames,
            prebuffer_seconds=prebuffer_seconds,
            missed_deadlines=missed,
            crops_submitted=(
                0 if self.crop_dispatcher is None else self.crop_dispatcher.submitted
            ),
            crops_dropped=(
                0 if self.crop_dispatcher is None else self.crop_dispatcher.dropped
            ),
            stopped=cancellation.is_set(),
        )


def _release_frame(source: ReplaySource, frame: object) -> None:
    release = getattr(source, "release_frame", None)
    if release is not None:
        release(frame)


def _preview_frame(source: ReplaySource, frame: object):
    convert = getattr(source, "preview_frame", None)
    return frame if convert is None else convert(frame)
