"""Inference-attached numerical statistics capture for live sorting."""

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import cv2
import numpy as np

from .calibration import MetricPlaneCalibration
from .crop import CropPayload, InferenceStatisticsEvidence
from .detection import RawGreenDetector
from .models import BeanRef, FrameAnalysis, TrackSnapshot, TrackStatus
from .runtime_priority import apply_background_audit_thread_profile
from .statistics_features import (
    extract_view_features,
    extract_view_primitives,
    local_area_scale,
    numeric_summary,
    paired_features,
)
from .stereo import StereoCropPreparation, StereoPairMetadata

CAPTURE_SCHEMA = "beanoflight-live-statistics-capture/v2"
OBSERVATION_SCHEMA = "beanoflight-live-statistics-observation/v2"
BEAN_LEDGER_SCHEMA = "beanoflight-live-statistics-bean-ledger/v1"
MAXIMUM_SAMPLES_PER_BEAN = 2


@dataclass(frozen=True, slots=True)
class LiveStatisticsSettings:
    """Pressure bounds for live numerical evidence collection."""

    inference_attached: bool = True
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
    caml_detection_area_px: int = 0
    camr_refinement_area_px: int = 0
    caml_bbox_px: tuple[int, int, int, int] = (0, 0, 0, 0)
    caml_solidity: float = 0.0
    caml_detection_mean_green: float = 0.0
    inference_job_id: str = ""
    caml_component_origin_px: tuple[int, int] = (0, 0)
    camr_component_origin_px: tuple[int, int] = (0, 0)
    capture_path: str = "inference-attached"
    camr_measurement_available: bool = True
    fallback_reason: str = ""


class LiveStatisticsCollector:
    """Capture at most two stereo measurements per public bean.

    The default path attaches masks and native areas to inference selections,
    then reuses their already-materialized stereo images.  The historical
    independent calibrated-crop path remains available for controlled A/B
    comparisons only.
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
            (
                "inference_statistics_camr_component",
                "undistort_point",
                "stereo_calibration",
            )
            if self.settings.inference_attached
            else (
                "prepare_statistics_stereo_crop",
                "undistort_point",
                "stereo_calibration",
            )
        )
        if self.settings.inference_attached:
            required = (*required, "prepare_crop")
        missing = tuple(name for name in required if not hasattr(source, name))
        if missing:
            raise TypeError(f"statistics source is missing: {', '.join(missing)}")
        self.source = source
        self.detector = detector
        self.background_gray = background_gray
        self.calibration = calibration
        processing_profile = str(
            getattr(source, "crop_processing_profile", "ml-fast")
        )
        self._source_colour_domain = f"{processing_profile}-inference-bgr"
        self.output_directory = output_directory.expanduser().resolve()
        self.provenance = dict(provenance or {})
        configure_processing = getattr(source, "configure_statistics_processing", None)
        if not self.settings.inference_attached and configure_processing is not None:
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
        self._accepted_inference_jobs: set[str] = set()
        self._unattached_primary_candidates: dict[BeanRef, _StatisticsWork] = {}
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
        if self.settings.inference_attached:
            self.observe_tracks(analysis.tracks, analysis.frame_index)
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

    def attach_to_inference(
        self,
        frame: Any,
        analysis: FrameAnalysis,
        payloads: tuple[CropPayload, ...],
    ) -> tuple[CropPayload, ...]:
        """Attach masks and native measurements to selected inference crops.

        This method runs while the current RAW frame and detector labels are
        valid.  It performs no demosaic, calibration, colour conversion, or
        persistence; those images will be materialized once by inference.
        """

        self.observe_tracks(analysis.tracks, analysis.frame_index)
        if not self.settings.inference_attached or not payloads:
            return payloads
        tracks = {track.bean_ref: track for track in analysis.tracks}
        attached: list[CropPayload] = []
        for payload in payloads:
            sample_index = _inference_sample_index(payload)
            if not 1 <= sample_index <= MAXIMUM_SAMPLES_PER_BEAN:
                attached.append(payload)
                continue
            track = tracks.get(payload.job.bean_ref)
            if track is None and payload.sorting_context is not None:
                track = payload.sorting_context.track
            pair = payload.stereo_pair
            if track is None or not track.history or pair is None:
                with self._lock:
                    self._metrics["inference_attachment_unavailable"] += 1
                attached.append(payload)
                continue
            observation = track.history[-1]
            try:
                caml_component = self.detector.component_mask_evidence(
                    observation.detection
                )
                camr_component = self.source.inference_statistics_camr_component(
                    frame,
                    pair,
                )
                caml_mask, caml_origin = (
                    (None, (0, 0))
                    if caml_component is None
                    else caml_component
                )
                camr_mask, camr_origin = (
                    (None, (0, 0))
                    if camr_component is None
                    else camr_component
                )
            except Exception as exc:  # noqa: BLE001 - baseline remains usable
                caml_mask = None
                camr_mask = None
                caml_origin = (0, 0)
                camr_origin = (0, 0)
                with self._lock:
                    self._metrics["inference_mask_errors"] += 1
                    self._failure_example(
                        payload.job.bean_ref,
                        "inference_mask_error",
                        str(exc),
                    )
            evidence = InferenceStatisticsEvidence(
                sample_index=sample_index,
                fov_band=self._fov_band(observation.position_mm[1]),
                track_status=track.status.value,
                track_hits=track.hits,
                caml_detection_area_px=observation.detection.area_px,
                camr_refinement_area_px=pair.refinement_area_px,
                caml_bbox_px=observation.detection.bbox_px,
                caml_solidity=observation.detection.solidity,
                caml_detection_mean_green=observation.detection.mean_bgr[1],
                caml_mask=caml_mask,
                camr_mask=camr_mask,
                caml_component_origin_px=caml_origin,
                camr_component_origin_px=camr_origin,
            )
            with self._lock:
                self._metrics["inference_samples_attached"] += 1
                if caml_mask is None:
                    self._metrics["inference_caml_mask_unavailable"] += 1
                if camr_mask is None:
                    self._metrics["inference_camr_mask_unavailable"] += 1
            attached.append(payload.with_statistics_evidence(evidence))
        return tuple(attached)

    def ingest_materialized(
        self, payloads: tuple[CropPayload, ...]
    ) -> None:
        """Accept successfully delivered, already-materialized inference crops."""

        if not self._threads or not self.settings.inference_attached:
            return
        for payload in payloads:
            evidence = payload.statistics_evidence
            pair = payload.stereo_pair
            if evidence is None or pair is None:
                continue
            if payload.image_bgr is None or payload.camr_image_bgr is None:
                with self._lock:
                    self._metrics["inference_images_unavailable"] += 1
                continue
            with self._lock:
                if payload.job.job_id in self._accepted_inference_jobs:
                    continue
                state = self._states.get(payload.job.bean_ref)
                if state is None:
                    self._metrics["inference_state_unavailable"] += 1
                    continue
                reserved = state.successful_samples + state.pending_samples
                if reserved >= self.settings.target_samples_per_bean:
                    continue
                primary = reserved == 0
                priority = 0 if primary else 1
                if not primary and not self._has_capacity_locked(priority):
                    self._metrics["secondary_capacity_drop"] += 1
                    self._state_failure(state, "secondary_capacity_drop")
                    continue
                prepared = StereoCropPreparation(
                    lambda image=payload.image_bgr: image,
                    lambda image=payload.camr_image_bgr: image,
                    payload.job.crop_width_px,
                    payload.job.crop_height_px,
                    payload.job.source_crop_width_px,
                    payload.job.padded,
                    pair,
                    evidence.camr_mask,
                )
                work = _StatisticsWork(
                    bean_ref=payload.job.bean_ref,
                    requested_sample_index=reserved + 1,
                    fov_band=evidence.fov_band,
                    frame_index=payload.job.frame_index,
                    timestamp_ns=payload.job.capture_timestamp_ns,
                    track_status=evidence.track_status,
                    track_hits=evidence.track_hits,
                    prepared=prepared,
                    caml_mask=(
                        evidence.caml_mask
                        if evidence.caml_mask is not None
                        else np.zeros((0, 0), dtype=np.uint8)
                    ),
                    camr_mask=(
                        evidence.camr_mask
                        if evidence.camr_mask is not None
                        else np.zeros((0, 0), dtype=np.uint8)
                    ),
                    enqueued_monotonic_ns=time.monotonic_ns(),
                    caml_detection_area_px=evidence.caml_detection_area_px,
                    camr_refinement_area_px=evidence.camr_refinement_area_px,
                    caml_bbox_px=evidence.caml_bbox_px,
                    caml_solidity=evidence.caml_solidity,
                    caml_detection_mean_green=(
                        evidence.caml_detection_mean_green
                    ),
                    inference_job_id=payload.job.job_id,
                    caml_component_origin_px=(
                        evidence.caml_component_origin_px
                    ),
                    camr_component_origin_px=(
                        evidence.camr_component_origin_px
                    ),
                )
                self._sequence += 1
                queued = (priority, self._sequence, work)
                state.pending_samples += 1
                state.last_reserved_frame = work.frame_index
                self._accepted_inference_jobs.add(payload.job.job_id)
            try:
                self._queue.put_nowait(queued)
            except queue.Full:
                if primary:
                    self._persist_primary_fallback(work, "primary_queue_saturated")
                else:
                    self._complete(
                        work,
                        succeeded=False,
                        reason="secondary_capacity_drop",
                    )
            else:
                with self._lock:
                    key = (
                        "primary_jobs_queued"
                        if primary
                        else "secondary_jobs_queued"
                    )
                    self._metrics[key] += 1
                    self._maximum_queue_depth = max(
                        self._maximum_queue_depth, self._queue.qsize()
                    )

    def cache_unattached_primary(
        self,
        frame: Any,
        analysis: FrameAnalysis,
        payloads: tuple[CropPayload, ...],
    ) -> None:
        """Retain one rare CamL fallback for a bean with no stereo sample.

        Normal beans reuse inference crops and never enter this path.  A
        confirmed bean can nevertheless be fully visible in CamL while CamR
        local refinement is unavailable for its entire flight.  Preserve one
        deferred compact CamL crop, then use it at shutdown only if inference
        still produced no statistics observation for that bean.
        """

        if (
            not self._threads
            or not self.settings.inference_attached
            or self._fatal_error
        ):
            return
        attached_refs = {
            payload.job.bean_ref
            for payload in payloads
            if payload.statistics_evidence is not None
        }
        for track in analysis.tracks:
            if track.status is not TrackStatus.CONFIRMED or not track.history:
                continue
            observation = track.history[-1]
            if observation.frame_index != analysis.frame_index:
                continue
            with self._lock:
                state = self._states.get(track.bean_ref)
                if (
                    state is None
                    or track.bean_ref in attached_refs
                    or state.successful_samples + state.pending_samples > 0
                ):
                    continue
                existing = self._unattached_primary_candidates.get(track.bean_ref)
                if (
                    existing is not None
                    and existing.fallback_reason
                    == "stereo-inference-unavailable"
                ):
                    continue
            prepared_crop = None
            component = None
            preparation_error = ""
            source_size_px = self.settings.crop_size_px
            try:
                prepared_crop = self.source.prepare_crop(
                    frame,
                    observation.detection.centroid_px,
                    source_size_px,
                    allow_padding=False,
                )
                if prepared_crop is None:
                    source_size_px = _largest_complete_component_crop(
                        observation.detection.centroid_px,
                        observation.detection.bbox_px,
                        (
                            int(self.source.metadata.width),
                            int(self.source.metadata.height),
                        ),
                        self.settings.crop_size_px,
                    )
                    if source_size_px:
                        prepared_crop = self.source.prepare_crop(
                            frame,
                            observation.detection.centroid_px,
                            source_size_px,
                            allow_padding=False,
                        )
                component = self.detector.component_mask_evidence(
                    observation.detection
                )
                if prepared_crop is None:
                    preparation_error = "caml_crop_unavailable"
                if component is None:
                    preparation_error = "caml_mask_unavailable"
            except Exception as exc:  # noqa: BLE001 - baseline is still useful
                preparation_error = str(exc)
            materializer = (
                _unavailable_materializer
                if prepared_crop is None
                else prepared_crop[0]
            )
            width_px = (
                self.settings.crop_size_px
                if prepared_crop is None
                else prepared_crop[1]
            )
            height_px = (
                self.settings.crop_size_px
                if prepared_crop is None
                else prepared_crop[2]
            )
            padded = False if prepared_crop is None else prepared_crop[3]
            caml_mask, caml_origin = (
                (np.zeros((0, 0), dtype=np.uint8), (0, 0))
                if component is None
                else component
            )
            pair = StereoPairMetadata(
                left_frame_index=observation.frame_index,
                right_frame_index=observation.frame_index,
                left_timestamp_ns=observation.timestamp_ns,
                right_timestamp_ns=observation.timestamp_ns,
                caml_centroid_px=observation.detection.centroid_px,
                camr_projected_centroid_px=(0.0, 0.0),
                camr_centroid_px=(0.0, 0.0),
                refinement_distance_px=0.0,
                refinement_area_px=0,
                coordinate_domain="caml-only-fallback",
            )
            work = _StatisticsWork(
                bean_ref=track.bean_ref,
                requested_sample_index=1,
                fov_band=self._fov_band(observation.position_mm[1]),
                frame_index=observation.frame_index,
                timestamp_ns=observation.timestamp_ns,
                track_status=track.status.value,
                track_hits=track.hits,
                prepared=StereoCropPreparation(
                    materializer,
                    _unavailable_materializer,
                    width_px,
                    height_px,
                    source_size_px or self.settings.crop_size_px,
                    padded,
                    pair,
                    None,
                ),
                caml_mask=caml_mask,
                camr_mask=np.zeros((0, 0), dtype=np.uint8),
                enqueued_monotonic_ns=0,
                caml_detection_area_px=observation.detection.area_px,
                caml_bbox_px=observation.detection.bbox_px,
                caml_solidity=observation.detection.solidity,
                caml_detection_mean_green=observation.detection.mean_bgr[1],
                caml_component_origin_px=caml_origin,
                capture_path="caml-only-zero-sample-fallback",
                camr_measurement_available=False,
                fallback_reason=(
                    preparation_error or "stereo-inference-unavailable"
                ),
            )
            with self._lock:
                existing = self._unattached_primary_candidates.get(track.bean_ref)
                if (
                    existing is not None
                    and existing.fallback_reason
                    == "stereo-inference-unavailable"
                ):
                    return
                self._unattached_primary_candidates[track.bean_ref] = work
                key = (
                    "caml_fallback_candidates_cached"
                    if existing is None
                    else "caml_fallback_candidates_upgraded"
                )
                self._metrics[key] += 1
            # One preparation per frame is enough. Other candidates remain
            # visible and can be retained on their next confirmed frame.
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
        if drain and self.settings.inference_attached:
            self._enqueue_zero_sample_fallbacks()
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
                "crop_materialization_ms": numeric_summary(
                    self._materialization_ms
                ),
                "feature_kernel_ms": numeric_summary(
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
                self._metrics["primary_jobs_queued"] += 1
            else:
                self._metrics["secondary_jobs_queued"] += 1
            self._maximum_queue_depth = max(
                self._maximum_queue_depth, self._queue.qsize()
            )
        return True

    def _enqueue_zero_sample_fallbacks(self) -> None:
        """Enqueue cached CamL evidence after inference has fully drained."""

        with self._lock:
            candidates = tuple(self._unattached_primary_candidates.items())
        for bean_ref, cached in candidates:
            with self._lock:
                state = self._states.get(bean_ref)
                if (
                    state is None
                    or not state.confirmed
                    or state.successful_samples + state.pending_samples > 0
                ):
                    continue
                work = replace(
                    cached,
                    requested_sample_index=1,
                    enqueued_monotonic_ns=time.monotonic_ns(),
                )
                self._sequence += 1
                queued = (0, self._sequence, work)
                state.pending_samples += 1
                state.last_reserved_frame = work.frame_index
            try:
                self._queue.put_nowait(queued)
            except queue.Full:
                self._persist_primary_fallback(
                    work,
                    "caml_fallback_queue_saturated",
                )
            else:
                with self._lock:
                    self._metrics["caml_fallback_jobs_queued"] += 1
                    self._maximum_queue_depth = max(
                        self._maximum_queue_depth,
                        self._queue.qsize(),
                    )
        with self._lock:
            uncovered = sum(
                state.confirmed
                and state.successful_samples + state.pending_samples == 0
                for state in self._states.values()
            )
            if uncovered:
                self._metrics["beans_without_recoverable_sample"] += uncovered

    def _has_capacity_locked(self, priority: int) -> bool:
        depth = self._queue.qsize()
        if priority == 0:
            return depth < self.settings.queue_capacity
        return depth < self.settings.queue_capacity - self.settings.primary_queue_reserve

    def _run(self, worker_index: int) -> None:
        cpu_from_end = worker_index + 1
        apply_background_audit_thread_profile(cpu_from_end=cpu_from_end)
        helper = (
            None
            if self.settings.inference_attached
            else ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="statistics-camr",
                initializer=lambda: apply_background_audit_thread_profile(
                    cpu_from_end=cpu_from_end
                ),
            )
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
                    if self.settings.inference_attached:
                        try:
                            self._persist(
                                self._baseline_row(
                                    work,
                                    enrichment_error=str(exc),
                                )
                            )
                        except Exception as fallback_exc:  # noqa: BLE001
                            self._complete(
                                work,
                                succeeded=False,
                                reason="measurement_error",
                                detail=str(fallback_exc),
                                primary=priority == 0,
                            )
                        else:
                            with self._lock:
                                self._metrics[
                                    "feature_enrichment_fallbacks"
                                ] += 1
                                self._failure_example(
                                    work.bean_ref,
                                    "feature_enrichment_fallback",
                                    str(exc),
                                )
                            self._complete(
                                work,
                                succeeded=True,
                                primary=priority == 0,
                            )
                    else:
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
            if helper is not None:
                helper.shutdown(wait=True, cancel_futures=True)

    def _measure(
        self,
        work: _StatisticsWork,
        helper: ThreadPoolExecutor | None,
    ) -> tuple[dict[str, object], dict[str, float]]:
        if self.settings.inference_attached:
            if not work.camr_measurement_available:
                return self._measure_caml_only(work)
            return self._measure_inference_attached(work)
        if helper is None:
            raise RuntimeError("legacy statistics helper is unavailable")
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
            "capture_path": (
                "inference-attached"
                if self.settings.inference_attached
                else "independent-calibrated-crop"
            ),
            "source_colour_domain": "calibrated-srgb-bgr",
            "inference_job_id": work.inference_job_id,
            "measurement_view_count": 2,
            "caml_measurement_available": True,
            "camr_measurement_available": True,
            "fallback_reason": "",
            "caml_detection_area_px": work.caml_detection_area_px,
            "camr_refinement_area_px": work.camr_refinement_area_px,
            "caml_detection_bbox_px": list(work.caml_bbox_px),
            "caml_detection_touches_sensor_edge": (
                self._caml_detection_touches_sensor_edge(work.caml_bbox_px)
            ),
            "caml_detection_solidity": work.caml_solidity,
            "caml_detection_mean_green": work.caml_detection_mean_green,
            "feature_enrichment_valid": True,
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

    def _measure_caml_only(
        self,
        work: _StatisticsWork,
    ) -> tuple[dict[str, object], dict[str, float]]:
        """Measure a rare CamL-only fallback without fabricating CamR data."""

        materialization_started = time.perf_counter_ns()
        left_image = work.prepared.caml_materializer()
        materialization_ms = (
            time.perf_counter_ns() - materialization_started
        ) / 1_000_000.0
        left_mask = _align_component_mask(
            work.caml_mask,
            work.caml_component_origin_px,
            work.prepared.pair.caml_centroid_px,
            work.prepared.source_size_px,
            work.prepared.width_px,
        )
        feature_started = time.perf_counter_ns()
        left = extract_view_primitives(left_image, left_mask)
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
            "camr_centroid_x_px": None,
            "camr_centroid_y_px": None,
            "camr_projected_x_px": None,
            "camr_projected_y_px": None,
            "right_frame_index": None,
            "synchronization_delta_ns": None,
            "source_crop_size_px": work.prepared.source_size_px,
            "inference_crop_width_px": None,
            "inference_crop_height_px": None,
            "mask_scale_to_native": (
                work.prepared.source_size_px
                / max(float(work.prepared.width_px), 1.0)
            ),
            "queue_wait_ms": queue_wait_ms,
            "materialization_ms": materialization_ms,
            "feature_kernel_ms": feature_kernel_ms,
            "feature_kernel_cpu_ms": left.kernel_ms,
            "refinement_distance_px": None,
            "capture_path": work.capture_path,
            "source_colour_domain": self._source_colour_domain,
            "inference_job_id": "",
            "measurement_view_count": 1,
            "caml_measurement_available": True,
            "camr_measurement_available": False,
            "fallback_reason": work.fallback_reason,
            "caml_detection_area_px": work.caml_detection_area_px,
            "camr_refinement_area_px": None,
            "projected_area_geomean_px": None,
            "projected_area_ratio_camr_to_caml": None,
            "single_view_area_proxy_px": work.caml_detection_area_px,
            "caml_detection_bbox_px": list(work.caml_bbox_px),
            "caml_detection_touches_sensor_edge": (
                self._caml_detection_touches_sensor_edge(work.caml_bbox_px)
            ),
            "caml_detection_solidity": work.caml_solidity,
            "caml_detection_mean_green": work.caml_detection_mean_green,
            "feature_enrichment_valid": True,
        }
        row.update({f"caml_{key}": value for key, value in left.values.items()})
        return row, {
            "materialization_ms": materialization_ms,
            "feature_kernel_ms": feature_kernel_ms,
        }

    def _measure_inference_attached(
        self,
        work: _StatisticsWork,
    ) -> tuple[dict[str, object], dict[str, float]]:
        materialization_started = time.perf_counter_ns()
        left_image = work.prepared.caml_materializer()
        right_image = work.prepared.camr_materializer()
        materialization_ms = (
            time.perf_counter_ns() - materialization_started
        ) / 1_000_000.0
        left_mask = _align_component_mask(
            work.caml_mask,
            work.caml_component_origin_px,
            work.prepared.pair.caml_centroid_px,
            work.prepared.source_size_px,
            work.prepared.width_px,
        )
        right_mask = _align_component_mask(
            work.camr_mask,
            work.camr_component_origin_px,
            work.prepared.pair.camr_centroid_px,
            work.prepared.source_size_px,
            work.prepared.width_px,
        )
        feature_started = time.perf_counter_ns()
        # Both audit threads deliberately share the same low-priority safety
        # CPU. Running the two tiny kernels sequentially avoids scheduler
        # ping-pong without consuming a sorting/general-purpose core.
        left = extract_view_primitives(left_image, left_mask)
        right = extract_view_primitives(right_image, right_mask)
        feature_kernel_ms = (
            time.perf_counter_ns() - feature_started
        ) / 1_000_000.0
        queue_wait_ms = (
            time.monotonic_ns() - work.enqueued_monotonic_ns
        ) / 1_000_000.0
        self._queue_wait_ms.append(queue_wait_ms)
        pair = work.prepared.pair
        left_area = float(work.caml_detection_area_px)
        right_area = float(work.camr_refinement_area_px)
        area_geomean = math.sqrt(max(0.0, left_area * right_area))
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
            "inference_crop_width_px": work.prepared.width_px,
            "inference_crop_height_px": work.prepared.height_px,
            "mask_scale_to_native": (
                work.prepared.source_size_px
                / max(float(work.prepared.width_px), 1.0)
            ),
            "queue_wait_ms": queue_wait_ms,
            "materialization_ms": materialization_ms,
            "feature_kernel_ms": feature_kernel_ms,
            "feature_kernel_cpu_ms": left.kernel_ms + right.kernel_ms,
            "refinement_distance_px": pair.refinement_distance_px,
            "capture_path": "inference-attached",
            "source_colour_domain": self._source_colour_domain,
            "inference_job_id": work.inference_job_id,
            "measurement_view_count": 2,
            "caml_measurement_available": True,
            "camr_measurement_available": True,
            "fallback_reason": "",
            "caml_detection_area_px": work.caml_detection_area_px,
            "camr_refinement_area_px": work.camr_refinement_area_px,
            "projected_area_geomean_px": area_geomean,
            "projected_area_ratio_camr_to_caml": (
                right_area / max(left_area, 1.0)
            ),
            "caml_detection_bbox_px": list(work.caml_bbox_px),
            "caml_detection_touches_sensor_edge": (
                self._caml_detection_touches_sensor_edge(work.caml_bbox_px)
            ),
            "caml_detection_solidity": work.caml_solidity,
            "caml_detection_mean_green": work.caml_detection_mean_green,
            "feature_enrichment_valid": True,
        }
        row.update({f"caml_{key}": value for key, value in left.values.items()})
        row.update({f"camr_{key}": value for key, value in right.values.items()})
        return row, {
            "materialization_ms": materialization_ms,
            "feature_kernel_ms": feature_kernel_ms,
        }

    def _baseline_row(
        self,
        work: _StatisticsWork,
        *,
        enrichment_error: str,
    ) -> dict[str, object]:
        """Preserve the mandatory per-inference sample when enrichment fails."""

        pair = work.prepared.pair
        area_geomean = (
            math.sqrt(
                max(0, work.caml_detection_area_px)
                * max(0, work.camr_refinement_area_px)
            )
            if work.camr_measurement_available
            else None
        )
        return {
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
            "camr_centroid_x_px": (
                pair.camr_centroid_px[0]
                if work.camr_measurement_available
                else None
            ),
            "camr_centroid_y_px": (
                pair.camr_centroid_px[1]
                if work.camr_measurement_available
                else None
            ),
            "camr_projected_x_px": (
                pair.camr_projected_centroid_px[0]
                if work.camr_measurement_available
                else None
            ),
            "camr_projected_y_px": (
                pair.camr_projected_centroid_px[1]
                if work.camr_measurement_available
                else None
            ),
            "right_frame_index": (
                pair.right_frame_index
                if work.camr_measurement_available
                else None
            ),
            "synchronization_delta_ns": (
                pair.synchronization_delta_ns
                if work.camr_measurement_available
                else None
            ),
            "source_crop_size_px": work.prepared.source_size_px,
            "refinement_distance_px": (
                pair.refinement_distance_px
                if work.camr_measurement_available
                else None
            ),
            "capture_path": work.capture_path,
            "source_colour_domain": self._source_colour_domain,
            "inference_job_id": work.inference_job_id,
            "measurement_view_count": (
                2 if work.camr_measurement_available else 1
            ),
            "caml_measurement_available": True,
            "camr_measurement_available": work.camr_measurement_available,
            "fallback_reason": work.fallback_reason,
            "caml_detection_area_px": work.caml_detection_area_px,
            "camr_refinement_area_px": work.camr_refinement_area_px,
            "projected_area_geomean_px": area_geomean,
            "single_view_area_proxy_px": (
                None
                if work.camr_measurement_available
                else work.caml_detection_area_px
            ),
            "caml_detection_bbox_px": list(work.caml_bbox_px),
            "caml_detection_touches_sensor_edge": (
                self._caml_detection_touches_sensor_edge(work.caml_bbox_px)
            ),
            "caml_detection_solidity": work.caml_solidity,
            "caml_detection_mean_green": work.caml_detection_mean_green,
            "feature_enrichment_valid": False,
            "feature_enrichment_error": enrichment_error,
        }

    def _persist_primary_fallback(
        self,
        work: _StatisticsWork,
        reason: str,
    ) -> None:
        """Synchronously preserve sample one if the bounded worker is full."""

        try:
            self._persist(self._baseline_row(work, enrichment_error=reason))
        except Exception as exc:  # noqa: BLE001 - converted to collector failure
            self._complete(
                work,
                succeeded=False,
                reason=reason,
                detail=str(exc),
                primary=True,
            )
            return
        with self._lock:
            self._metrics["primary_synchronous_fallbacks"] += 1
            self._failure_example(work.bean_ref, reason, "")
        self._complete(work, succeeded=True, primary=True)

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
            if succeeded and state.successful_samples < MAXIMUM_SAMPLES_PER_BEAN:
                state.successful_samples += 1
                state.successful_bands.append(work.fov_band)
                self._metrics["observations_written"] += 1
                if not work.camr_measurement_available:
                    self._metrics["caml_fallback_observations_written"] += 1
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

    def _caml_detection_touches_sensor_edge(
        self,
        bbox_px: tuple[int, int, int, int],
    ) -> bool:
        x, y, width, height = bbox_px
        metadata = self.source.metadata
        return (
            x <= 0
            or y <= 0
            or x + width >= int(metadata.width)
            or y + height >= int(metadata.height)
        )

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
                "capture_path": (
                    "inference-attached"
                    if self.settings.inference_attached
                    else "independent-calibrated-crop"
                ),
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
                "online_features": (
                    "masked BGR sums/sums-of-squares, pixel counts and "
                    "silhouette spatial moments"
                ),
                "offline_features": (
                    "colour normalization, perceptual spaces, ellipse "
                    "dimensions and volume proxies"
                ),
            },
            "provenance": self.provenance,
            "statistics": self.statistics(),
            "files": files,
        }
        _write_json_atomic(self.output_directory / "capture.json", payload)

    def _warm_feature_kernel(self) -> None:
        image = np.zeros((64, 64, 3), dtype=np.uint8)
        mask = np.zeros((64, 64), dtype=np.uint8)
        cv2.ellipse(mask, (32, 32), (12, 8), 0, 0, 360, 255, -1)
        image[mask > 0] = (60, 110, 180)
        if self.settings.inference_attached:
            extract_view_primitives(image, mask)
        else:
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


def _unavailable_materializer() -> np.ndarray:
    raise ValueError("fallback image materialization is unavailable")


def _largest_complete_component_crop(
    centroid_px: tuple[float, float],
    bbox_px: tuple[int, int, int, int],
    frame_size_px: tuple[int, int],
    target_size_px: int,
) -> int | None:
    """Return a complete even crop that still contains the whole component."""

    centre_x, centre_y = centroid_px
    frame_width, frame_height = frame_size_px
    radius = math.floor(
        min(
            centre_x,
            centre_y,
            frame_width - centre_x,
            frame_height - centre_y,
        )
    )
    available = min(target_size_px, max(0, radius * 2))
    available -= available % 2
    x, y, width, height = bbox_px
    required_radius = max(
        centre_x - x,
        x + width - centre_x,
        centre_y - y,
        y + height - centre_y,
    )
    required = math.ceil(required_radius * 2)
    required += required % 2
    return available if available >= max(32, required) else None


def _inference_sample_index(payload: CropPayload) -> int:
    try:
        return int(payload.job.job_id.rsplit(":", 1)[1]) + 1
    except (IndexError, ValueError):
        return 1


def _align_component_mask(
    component: np.ndarray,
    component_origin_px: tuple[int, int],
    crop_centroid_px: tuple[float, float],
    source_size_px: int,
    target_size_px: int,
) -> np.ndarray:
    if (
        component.dtype != np.uint8
        or component.ndim != 2
        or component.size == 0
        or source_size_px <= 0
        or target_size_px <= 0
    ):
        raise ValueError("statistics component mask is unavailable")
    crop_left = round(crop_centroid_px[0]) - source_size_px // 2
    crop_top = round(crop_centroid_px[1]) - source_size_px // 2
    component_left, component_top = component_origin_px
    component_height, component_width = component.shape
    source_left = max(component_left, crop_left)
    source_top = max(component_top, crop_top)
    source_right = min(
        component_left + component_width,
        crop_left + source_size_px,
    )
    source_bottom = min(
        component_top + component_height,
        crop_top + source_size_px,
    )
    if source_left >= source_right or source_top >= source_bottom:
        raise ValueError("statistics component does not intersect inference crop")
    mask = np.zeros((source_size_px, source_size_px), dtype=np.uint8)
    mask[
        source_top - crop_top : source_bottom - crop_top,
        source_left - crop_left : source_right - crop_left,
    ] = component[
        source_top - component_top : source_bottom - component_top,
        source_left - component_left : source_right - component_left,
    ]
    if target_size_px != source_size_px:
        mask = cv2.resize(
            mask,
            (target_size_px, target_size_px),
            interpolation=cv2.INTER_NEAREST,
        )
    return np.ascontiguousarray(mask)


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
