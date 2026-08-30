"""Best-effort numerical statistics capture for the live sorting pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import queue
import threading
import time
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import cv2
import numpy as np

from .calibration import MetricPlaneCalibration
from .detection import RawGreenDetector
from .models import BeanRef, FrameAnalysis, TrackSnapshot, TrackStatus
from .runtime_priority import apply_background_audit_thread_profile
from .statistics_features import (
    extract_view_features,
    local_area_scale,
    numeric_summary,
    paired_features,
)
from .stereo import StereoCropPreparation

CAPTURE_SCHEMA = "beanoflight-live-statistics-capture/v1"
OBSERVATION_SCHEMA = "beanoflight-statistics-observation/v1"
BEAN_LEDGER_SCHEMA = "beanoflight-live-statistics-bean-ledger/v1"
MAXIMUM_SAMPLES_PER_BEAN = 2


@dataclass(frozen=True, slots=True)
class LiveStatisticsSettings:
    """Pressure bounds for optional live numerical evidence collection."""

    crop_size_px: int = 160
    target_samples_per_bean: int = MAXIMUM_SAMPLES_PER_BEAN
    queue_capacity: int = 24
    primary_queue_reserve: int = 8
    worker_count: int = 1
    maximum_preparations_per_frame: int = 1
    maximum_preparation_start_ms: float = 10.0
    minimum_sample_frame_gap: int = 1
    flush_every: int = 32

    def validate(self) -> None:
        if self.crop_size_px < 64 or self.crop_size_px % 2:
            raise ValueError("statistics crop size must be an even integer of at least 64")
        if not 1 <= self.target_samples_per_bean <= MAXIMUM_SAMPLES_PER_BEAN:
            raise ValueError("live statistics supports one or two samples per bean only")
        if self.queue_capacity <= 0:
            raise ValueError("statistics queue capacity must be positive")
        if not 0 <= self.primary_queue_reserve < self.queue_capacity:
            raise ValueError(
                "statistics primary reserve must be non-negative and below capacity"
            )
        if not 1 <= self.worker_count <= 2:
            raise ValueError("statistics worker count must be one or two")
        if self.maximum_preparations_per_frame != 1:
            raise ValueError(
                "live statistics permits exactly one ROI preparation per frame"
            )
        if (
            not math.isfinite(self.maximum_preparation_start_ms)
            or self.maximum_preparation_start_ms <= 0
        ):
            raise ValueError("statistics preparation start budget must be positive")
        if self.minimum_sample_frame_gap < 1:
            raise ValueError("statistics sample frame gap must be positive")
        if self.flush_every < 1:
            raise ValueError("statistics flush interval must be positive")


@dataclass(slots=True)
class _BeanCaptureState:
    first_frame_index: int
    last_frame_index: int
    first_timestamp_ns: int
    last_timestamp_ns: int
    maximum_hits: int
    terminal_status: str
    confirmed: bool = False
    successful_samples: int = 0
    pending_samples: int = 0
    successful_bands: list[int] = field(default_factory=list)
    last_reserved_frame: int | None = None
    failures: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _StatisticsWork:
    bean_ref: BeanRef
    requested_sample_index: int
    fov_band: int
    frame_index: int
    timestamp_ns: int
    track_status: str
    track_hits: int
    prepared: StereoCropPreparation = field(compare=False, repr=False)
    caml_mask: np.ndarray = field(compare=False, repr=False)
    camr_mask: np.ndarray = field(compare=False, repr=False)
    enqueued_monotonic_ns: int


class LiveStatisticsCollector:
    """Capture at most two calibrated stereo measurements per public bean.

    The frame thread only reserves a bounded queue slot, copies two RAW ROIs,
    copies the existing CamL component mask, and exposes CamR's compact green
    plane. Calibration, feature extraction and persistence run on a lowered-
    priority worker. Queue admission never waits.
    """

    def __init__(
        self,
        source: Any,
        detector: RawGreenDetector,
        background_gray: np.ndarray,
        calibration: MetricPlaneCalibration,
        output_directory: Path,
        *,
        settings: LiveStatisticsSettings | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> None:
        self.settings = settings or LiveStatisticsSettings()
        self.settings.validate()
        if not isinstance(detector, RawGreenDetector):
            raise TypeError("live statistics requires the RAW green detector")
        required = (
            "prepare_statistics_stereo_crop",
            "undistort_point",
            "stereo_calibration",
        )
        missing = tuple(name for name in required if not hasattr(source, name))
        if missing:
            raise TypeError(f"statistics source is missing: {', '.join(missing)}")
        self.source = source
        self.detector = detector
        self.background_gray = background_gray
        self.calibration = calibration
        self.output_directory = output_directory.expanduser().resolve()
        self.provenance = dict(provenance or {})
        configure_processing = getattr(source, "configure_statistics_processing", None)
        if configure_processing is not None:
            configure_processing()
        self._queue: queue.PriorityQueue[tuple[int, int, _StatisticsWork]] = (
            queue.PriorityQueue(maxsize=self.settings.queue_capacity)
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._write_lock = threading.Lock()
        self._stream: TextIO | None = None
        self._run_id = ""
        self._started_utc = ""
        self._sequence = 0
        self._states: dict[BeanRef, _BeanCaptureState] = {}
        self._primary_pending = 0
        self._metrics: defaultdict[str, int] = defaultdict(int)
        self._failure_examples: list[dict[str, object]] = []
        self._job_wall_ms: list[float] = []
        self._queue_wait_ms: list[float] = []
        self._materialization_ms: list[float] = []
        self._feature_kernel_ms: list[float] = []
        self._maximum_queue_depth = 0
        self._fatal_error = ""

    def start(self, run_id: str) -> None:
        if self._threads:
            raise RuntimeError("live statistics collector has already started")
        if not run_id:
            raise ValueError("statistics run ID is required")
        self.output_directory.mkdir(parents=True, exist_ok=False)
        self._run_id = run_id
        self._started_utc = datetime.now(timezone.utc).isoformat()
        self._stream = (self.output_directory / "observations.jsonl").open(
            "x", encoding="utf-8", buffering=64 * 1024
        )
        self._write_capture_metadata("running")
        self._threads = [
            threading.Thread(
                target=self._run,
                args=(index,),
                name=f"beanoflight-live-statistics-{index + 1}",
                daemon=True,
            )
            for index in range(self.settings.worker_count)
        ]
        for thread in self._threads:
            thread.start()

    def consider(
        self,
        frame: Any,
        analysis: FrameAnalysis,
        *,
        allow_preparation: bool = True,
        preferred_bean_refs: frozenset[BeanRef] = frozenset(),
    ) -> None:
        """Offer current-frame tracks without ever waiting for worker capacity."""

        if not self._threads or self._fatal_error:
            return
        primary: list[tuple[TrackSnapshot, int]] = []
        secondary: list[tuple[TrackSnapshot, int]] = []
        with self._lock:
            for track in analysis.tracks:
                state = self._observe_track_locked(track, analysis.frame_index)
                if not track.history:
                    continue
                observation = track.history[-1]
                if observation.frame_index != analysis.frame_index:
                    continue
                reserved = state.successful_samples + state.pending_samples
                if reserved >= self.settings.target_samples_per_bean:
                    continue
                band = self._fov_band(observation.position_mm[1])
                if reserved == 0:
                    primary.append((track, band))
                    continue
                if reserved != 1 or state.last_reserved_frame is None:
                    continue
                if (
                    analysis.frame_index - state.last_reserved_frame
                    < self.settings.minimum_sample_frame_gap
                ):
                    continue
                secondary.append((track, band))
        primary.sort(
            key=lambda candidate: candidate[0].bean_ref not in preferred_bean_refs
        )
        secondary.sort(
            key=lambda candidate: candidate[0].bean_ref not in preferred_bean_refs
        )
        if not allow_preparation:
            if primary or secondary:
                with self._lock:
                    self._metrics["pressure_deferred_frames"] += 1
            return
        preparations = 0
        for track, band in primary:
            if self._try_enqueue(frame, track, band, secondary=False):
                preparations += 1
            if preparations >= self.settings.maximum_preparations_per_frame:
                return
        for track, band in secondary:
            if self._try_enqueue(frame, track, band, secondary=True):
                preparations += 1
            if preparations >= self.settings.maximum_preparations_per_frame:
                return

    def observe_tracks(
        self, tracks: tuple[TrackSnapshot, ...], frame_index: int
    ) -> None:
        """Retain terminal/right-censored track state for the coverage ledger."""

        with self._lock:
            for track in tracks:
                self._observe_track_locked(track, frame_index)

    def close(self, *, drain: bool = True, failed: bool = False) -> dict[str, object]:
        threads = tuple(self._threads)
        if not threads:
            return self.statistics()
        self._stop.set()
        if not drain:
            while True:
                try:
                    _priority, _sequence, work = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._complete(work, succeeded=False, reason="shutdown_discard")
                self._queue.task_done()
        for thread in threads:
            thread.join()
        self._threads.clear()
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.flush()
            stream.close()
        self._write_bean_ledger()
        status = "failed" if failed or self._fatal_error else "completed"
        self._write_capture_metadata(status)
        return self.statistics()

    def statistics(self) -> dict[str, object]:
        with self._lock:
            confirmed = tuple(
                state for state in self._states.values() if state.confirmed
            )
            samples = tuple(state.successful_samples for state in confirmed)
            all_samples = sum(
                state.successful_samples for state in self._states.values()
            )
            metrics = dict(sorted(self._metrics.items()))
        confirmed_samples = sum(samples)
        return {
            "enabled": True,
            "output_directory": str(self.output_directory),
            "target_samples_per_bean": self.settings.target_samples_per_bean,
            "confirmed_beans": len(confirmed),
            "beans_with_two_samples": sum(value == 2 for value in samples),
            "beans_with_one_sample": sum(value == 1 for value in samples),
            "beans_without_samples": sum(value == 0 for value in samples),
            "observations_persisted": confirmed_samples,
            "total_observations_persisted": all_samples,
            "unconfirmed_observations_persisted": (
                all_samples - confirmed_samples
            ),
            "maximum_queue_depth": self._maximum_queue_depth,
            "fatal_error": self._fatal_error,
            "counts": metrics,
            "performance": {
                "queue_wait_ms": numeric_summary(self._queue_wait_ms),
                "job_wall_ms": numeric_summary(self._job_wall_ms),
                "calibrated_crop_materialization_ms": numeric_summary(
                    self._materialization_ms
                ),
                "two_view_feature_kernel_ms": numeric_summary(
                    self._feature_kernel_ms
                ),
            },
            "failure_examples": list(self._failure_examples),
        }

    def _try_enqueue(
        self,
        frame: Any,
        track: TrackSnapshot,
        band: int,
        *,
        secondary: bool,
    ) -> bool:
        priority = 1 if secondary else 0
        with self._lock:
            state = self._states[track.bean_ref]
            if not self._has_capacity_locked(priority):
                key = "secondary_capacity_drop" if secondary else "primary_capacity_drop"
                self._metrics[key] += 1
                self._state_failure(state, key)
                return False
        observation = track.history[-1]
        try:
            prepared = self.source.prepare_statistics_stereo_crop(
                frame,
                observation.detection.centroid_px,
                self.settings.crop_size_px,
                allow_padding=False,
                allow_resize=True,
            )
            if prepared is None:
                self._record_prepare_failure(track.bean_ref, "stereo_crop_unavailable")
                return True
            left_mask = self.detector.component_crop_mask(
                observation.detection,
                prepared.pair.caml_centroid_px,
                prepared.source_size_px,
            )
            if left_mask is None:
                self._record_prepare_failure(track.bean_ref, "caml_mask_unavailable")
                return True
            right_mask = prepared.camr_mask
            if right_mask is None:
                self._record_prepare_failure(track.bean_ref, "camr_mask_unavailable")
                return True
        except Exception as exc:  # noqa: BLE001 - statistics must not break sorting
            self._record_prepare_failure(
                track.bean_ref,
                "preparation_error",
                detail=str(exc),
            )
            return True
        with self._lock:
            state = self._states[track.bean_ref]
            reserved = state.successful_samples + state.pending_samples
            if (
                reserved >= self.settings.target_samples_per_bean
                or not self._has_capacity_locked(priority)
            ):
                self._metrics["admission_race_drop"] += 1
                return True
            work = _StatisticsWork(
                bean_ref=track.bean_ref,
                requested_sample_index=reserved + 1,
                fov_band=band,
                frame_index=observation.frame_index,
                timestamp_ns=observation.timestamp_ns,
                track_status=track.status.value,
                track_hits=track.hits,
                prepared=prepared,
                caml_mask=left_mask,
                camr_mask=right_mask,
                enqueued_monotonic_ns=time.monotonic_ns(),
            )
            self._sequence += 1
            try:
                self._queue.put_nowait((priority, self._sequence, work))
            except queue.Full:
                self._metrics["admission_race_drop"] += 1
                return True
            state.pending_samples += 1
            state.last_reserved_frame = work.frame_index
            if priority == 0:
                self._primary_pending += 1
                self._metrics["primary_jobs_queued"] += 1
            else:
                self._metrics["secondary_jobs_queued"] += 1
            self._maximum_queue_depth = max(
                self._maximum_queue_depth, self._queue.qsize()
            )
        return True

    def _has_capacity_locked(self, priority: int) -> bool:
        depth = self._queue.qsize()
        if priority == 0:
            return depth < self.settings.queue_capacity
        return depth < self.settings.queue_capacity - self.settings.primary_queue_reserve

    def _run(self, worker_index: int) -> None:
        cpu_from_end = worker_index + 1
        apply_background_audit_thread_profile(cpu_from_end=cpu_from_end)
        helper = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="statistics-camr",
            initializer=lambda: apply_background_audit_thread_profile(
                cpu_from_end=cpu_from_end
            ),
        )
        try:
            self._warm_feature_kernel()
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    priority, _sequence, work = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                started = time.perf_counter_ns()
                try:
                    row, timings = self._measure(work, helper)
                    self._persist(row)
                except Exception as exc:  # noqa: BLE001 - optional worker isolation
                    self._complete(
                        work,
                        succeeded=False,
                        reason="measurement_error",
                        detail=str(exc),
                        primary=priority == 0,
                    )
                else:
                    wall_ms = (time.perf_counter_ns() - started) / 1_000_000.0
                    self._job_wall_ms.append(wall_ms)
                    self._materialization_ms.append(timings["materialization_ms"])
                    self._feature_kernel_ms.append(timings["feature_kernel_ms"])
                    self._complete(
                        work,
                        succeeded=True,
                        primary=priority == 0,
                    )
                finally:
                    self._queue.task_done()
        except Exception as exc:  # noqa: BLE001 - reported, never propagated to sorting
            self._fatal_error = str(exc)
            with self._lock:
                self._metrics["worker_fatal_errors"] += 1
        finally:
            helper.shutdown(wait=True, cancel_futures=True)

    def _measure(
        self,
        work: _StatisticsWork,
        helper: ThreadPoolExecutor,
    ) -> tuple[dict[str, object], dict[str, float]]:
        materialization_started = time.perf_counter_ns()
        right_image_future = helper.submit(work.prepared.camr_materializer)
        try:
            left_image = work.prepared.caml_materializer()
            right_image = right_image_future.result()
        except Exception:
            right_image_future.cancel()
            raise
        materialization_ms = (
            time.perf_counter_ns() - materialization_started
        ) / 1_000_000.0
        left_scale = local_area_scale(
            work.prepared.pair.caml_centroid_px,
            lambda point: self.calibration.pixel_to_mm(
                self.source.undistort_point(point)
            ),
        )
        right_scale = local_area_scale(
            work.prepared.pair.camr_centroid_px,
            lambda point: self.calibration.pixel_to_mm(
                self.source.stereo_calibration.project_distorted_camr_to_undistorted_caml(
                    point
                )
            ),
        )
        feature_started = time.perf_counter_ns()
        right_features_future = helper.submit(
            extract_view_features,
            right_image,
            work.camr_mask,
            area_scale_mm2_per_px=right_scale,
        )
        try:
            left_features = extract_view_features(
                left_image,
                work.caml_mask,
                area_scale_mm2_per_px=left_scale,
            )
            right_features = right_features_future.result()
        except Exception:
            right_features_future.cancel()
            raise
        feature_kernel_ms = (
            time.perf_counter_ns() - feature_started
        ) / 1_000_000.0
        queue_wait_ms = (
            time.monotonic_ns() - work.enqueued_monotonic_ns
        ) / 1_000_000.0
        self._queue_wait_ms.append(queue_wait_ms)
        pair = work.prepared.pair
        row: dict[str, object] = {
            "schema": OBSERVATION_SCHEMA,
            "bean_id": str(work.bean_ref),
            "run_id": work.bean_ref.run_id,
            "bean_sequence": work.bean_ref.sequence,
            "sample_index": work.requested_sample_index,
            "fov_band": ("top", "middle", "bottom")[work.fov_band],
            "frame_index": work.frame_index,
            "timestamp_ns": work.timestamp_ns,
            "track_status": work.track_status,
            "track_hits": work.track_hits,
            "caml_centroid_x_px": pair.caml_centroid_px[0],
            "caml_centroid_y_px": pair.caml_centroid_px[1],
            "camr_centroid_x_px": pair.camr_centroid_px[0],
            "camr_centroid_y_px": pair.camr_centroid_px[1],
            "camr_projected_x_px": pair.camr_projected_centroid_px[0],
            "camr_projected_y_px": pair.camr_projected_centroid_px[1],
            "right_frame_index": pair.right_frame_index,
            "synchronization_delta_ns": pair.synchronization_delta_ns,
            "source_crop_size_px": work.prepared.source_size_px,
            "camr_mask_domain": "single-green",
            "queue_wait_ms": queue_wait_ms,
            "materialization_ms": materialization_ms,
            "feature_kernel_ms": feature_kernel_ms,
            "feature_kernel_cpu_ms": (
                left_features.kernel_ms + right_features.kernel_ms
            ),
            "refinement_distance_px": pair.refinement_distance_px,
        }
        row.update(
            {f"caml_{key}": value for key, value in left_features.values.items()}
        )
        row.update(
            {f"camr_{key}": value for key, value in right_features.values.items()}
        )
        row.update(
            paired_features(
                left_features.values,
                right_features.values,
                pair.refinement_distance_px,
            )
        )
        return row, {
            "materialization_ms": materialization_ms,
            "feature_kernel_ms": feature_kernel_ms,
        }

    def _persist(self, row: Mapping[str, object]) -> None:
        stream = self._stream
        if stream is None:
            raise RuntimeError("statistics observation stream is closed")
        with self._write_lock:
            stream.write(json.dumps(_json_safe(row), sort_keys=True) + "\n")
            with self._lock:
                next_count = self._metrics["observations_written"] + 1
            if next_count % self.settings.flush_every == 0:
                stream.flush()

    def _complete(
        self,
        work: _StatisticsWork,
        *,
        succeeded: bool,
        primary: bool = False,
        reason: str = "",
        detail: str = "",
    ) -> None:
        with self._lock:
            state = self._states[work.bean_ref]
            state.pending_samples = max(0, state.pending_samples - 1)
            if primary:
                self._primary_pending = max(0, self._primary_pending - 1)
            if succeeded and state.successful_samples < MAXIMUM_SAMPLES_PER_BEAN:
                state.successful_samples += 1
                state.successful_bands.append(work.fov_band)
                self._metrics["observations_written"] += 1
            elif not succeeded:
                self._state_failure(state, reason)
                self._metrics[reason] += 1
                self._failure_example(work.bean_ref, reason, detail)

    def _observe_track_locked(
        self, track: TrackSnapshot, frame_index: int
    ) -> _BeanCaptureState:
        timestamp_ns = track.timestamp_ns
        state = self._states.get(track.bean_ref)
        if state is None:
            state = _BeanCaptureState(
                first_frame_index=frame_index,
                last_frame_index=frame_index,
                first_timestamp_ns=timestamp_ns,
                last_timestamp_ns=timestamp_ns,
                maximum_hits=track.hits,
                terminal_status=track.status.value,
            )
            self._states[track.bean_ref] = state
        state.last_frame_index = frame_index
        state.last_timestamp_ns = timestamp_ns
        state.maximum_hits = max(state.maximum_hits, track.hits)
        state.terminal_status = track.status.value
        state.confirmed = state.confirmed or track.status in {
            TrackStatus.CONFIRMED,
            TrackStatus.OCCLUDED,
            TrackStatus.EXITED,
        }
        return state

    def _record_prepare_failure(
        self, bean_ref: BeanRef, reason: str, *, detail: str = ""
    ) -> None:
        with self._lock:
            state = self._states[bean_ref]
            self._state_failure(state, reason)
            self._metrics[reason] += 1
            self._failure_example(bean_ref, reason, detail)

    @staticmethod
    def _state_failure(state: _BeanCaptureState, reason: str) -> None:
        state.failures[reason] = state.failures.get(reason, 0) + 1

    def _failure_example(self, bean_ref: BeanRef, reason: str, detail: str) -> None:
        if len(self._failure_examples) >= 12:
            return
        self._failure_examples.append(
            {"bean_id": str(bean_ref), "reason": reason, "detail": detail}
        )

    def _fov_band(self, y_mm: float) -> int:
        span = self.calibration.bottom_y_mm - self.calibration.top_y_mm
        normalized = (y_mm - self.calibration.top_y_mm) / span
        return min(2, max(0, int(normalized * 3.0)))

    def _write_bean_ledger(self) -> None:
        path = self.output_directory / "beans.jsonl"
        with path.open("x", encoding="utf-8") as stream:
            with self._lock:
                items = tuple(sorted(self._states.items()))
            for ref, state in items:
                if not state.confirmed:
                    continue
                row = {
                    "schema": BEAN_LEDGER_SCHEMA,
                    "bean_id": str(ref),
                    "run_id": ref.run_id,
                    "bean_sequence": ref.sequence,
                    "sample_count": state.successful_samples,
                    "target_sample_count": self.settings.target_samples_per_bean,
                    "single_sample_fallback": state.successful_samples == 1,
                    "sampled_fov_bands": [
                        ("top", "middle", "bottom")[band]
                        for band in state.successful_bands
                    ],
                    "first_frame_index": state.first_frame_index,
                    "last_frame_index": state.last_frame_index,
                    "first_timestamp_ns": state.first_timestamp_ns,
                    "last_timestamp_ns": state.last_timestamp_ns,
                    "track_maximum_hits": state.maximum_hits,
                    "terminal_status": state.terminal_status,
                    "collection_failures": dict(sorted(state.failures.items())),
                }
                stream.write(json.dumps(_json_safe(row), sort_keys=True) + "\n")

    def _write_capture_metadata(self, status: str) -> None:
        files: dict[str, object] = {}
        for name in ("observations.jsonl", "beans.jsonl"):
            path = self.output_directory / name
            if path.is_file():
                files[name] = {
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
        payload = {
            "schema": CAPTURE_SCHEMA,
            "status": status,
            "run_id": self._run_id,
            "started_utc": self._started_utc,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "settings": {
                "crop_size_px": self.settings.crop_size_px,
                "target_samples_per_bean": self.settings.target_samples_per_bean,
                "hard_maximum_samples_per_bean": MAXIMUM_SAMPLES_PER_BEAN,
                "queue_capacity": self.settings.queue_capacity,
                "primary_queue_reserve": self.settings.primary_queue_reserve,
                "worker_count": self.settings.worker_count,
                "maximum_preparations_per_frame": (
                    self.settings.maximum_preparations_per_frame
                ),
                "maximum_preparation_start_ms": (
                    self.settings.maximum_preparation_start_ms
                ),
                "minimum_sample_frame_gap": self.settings.minimum_sample_frame_gap,
                "persistence": "numerical JSON/JSONL only; no bean images",
            },
            "provenance": self.provenance,
            "statistics": self.statistics(),
            "files": files,
        }
        _write_json_atomic(self.output_directory / "capture.json", payload)

    @staticmethod
    def _warm_feature_kernel() -> None:
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        mask = np.zeros((64, 64), dtype=np.uint8)
        cv2.ellipse(mask, (32, 32), (12, 8), 0, 0, 360, 255, -1)
        image[mask > 0] = (60, 110, 180)
        extract_view_features(image, mask, area_scale_mm2_per_px=0.01)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
