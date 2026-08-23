"""Bounded, deterministic selection of lossless inference crops."""

from __future__ import annotations

import heapq
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from .models import BeanRef, FrameAnalysis, TrackStatus
from .registry_models import InferenceJob, InferenceStatus
from .stereo import StereoCropPreparation, StereoPairMetadata


@dataclass(frozen=True, slots=True)
class CropSettings:
    size_px: int = 224
    camera_id: str = "CamL"
    allow_padding: bool = False
    adaptive_edge_resize: bool = True
    max_crops_per_bean: int = 1

    def validate(self) -> None:
        if self.size_px <= 0:
            raise ValueError("crop size must be positive")
        if not self.camera_id.strip():
            raise ValueError("crop camera ID is required")
        if not 1 <= self.max_crops_per_bean <= 5:
            raise ValueError("crops per bean must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class CropPayload:
    job: InferenceJob
    image_bgr: np.ndarray | None = field(compare=False, repr=False)
    materializer: Callable[[], np.ndarray] | None = field(
        default=None, compare=False, repr=False
    )
    camr_image_bgr: np.ndarray | None = field(default=None, compare=False, repr=False)
    camr_materializer: Callable[[], np.ndarray] | None = field(
        default=None, compare=False, repr=False
    )
    stereo_pair: StereoPairMetadata | None = None

    @property
    def stereo_pair_complete(self) -> bool:
        return self.camr_image_bgr is not None and self.stereo_pair is not None

    def materialized(self, right_executor: Executor | None = None) -> CropPayload:
        if self.image_bgr is not None and (
            self.stereo_pair is None or self.camr_image_bgr is not None
        ):
            return self
        if self.image_bgr is None and self.materializer is None:
            raise ValueError("crop payload has neither an image nor a materializer")
        right_future = (
            right_executor.submit(self.camr_materializer)
            if right_executor is not None
            and self.stereo_pair is not None
            and self.camr_image_bgr is None
            and self.camr_materializer is not None
            else None
        )
        try:
            image = (
                self.image_bgr
                if self.image_bgr is not None
                else self.materializer()
            )
        except Exception:
            if right_future is not None:
                right_future.cancel()
            raise
        _validate_materialized_image(image, "CamL")
        right = self.camr_image_bgr
        if self.stereo_pair is not None:
            if right is None and self.camr_materializer is None:
                raise ValueError("stereo crop has no CamR image or materializer")
            right = (
                right
                if right is not None
                else (
                    right_future.result()
                    if right_future is not None
                    else self.camr_materializer()
                )
            )
            _validate_materialized_image(right, "CamR")
            if right.shape != image.shape:
                raise ValueError("CamL and CamR inference crop shapes differ")
        elif right is not None or self.camr_materializer is not None:
            raise ValueError("CamR crop requires stereo pair metadata")
        return CropPayload(
            self.job,
            image,
            None,
            right,
            None,
            self.stereo_pair,
        )

    def with_job(self, job: InferenceJob) -> CropPayload:
        return CropPayload(
            job,
            self.image_bgr,
            self.materializer,
            self.camr_image_bgr,
            self.camr_materializer,
            self.stereo_pair,
        )


class BeanCropSelector:
    """Choose the earliest valid observation for each public bean ID."""

    def __init__(
        self,
        settings: CropSettings | None = None,
        *,
        extractor: Callable[..., tuple[np.ndarray | None, bool]] | None = None,
        deferred_extractor: Callable[
            ..., tuple[Callable[[], np.ndarray], int, int, bool] | None
        ]
        | None = None,
        stereo_extractor: Callable[..., StereoCropPreparation | None] | None = None,
    ) -> None:
        self.settings = settings or CropSettings()
        self.settings.validate()
        self._extractor = extractor or extract_square_crop
        self._deferred_extractor = deferred_extractor
        self._stereo_extractor = stereo_extractor
        self._active_counts: dict[BeanRef, int] = {}
        self._next_indices: dict[BeanRef, int] = {}
        self._retry_indices: dict[BeanRef, list[int]] = {}
        self._pending: dict[str, tuple[BeanRef, int]] = {}
        self._lock = threading.Lock()
        self._stereo_attempts = 0
        self._stereo_selected = 0
        self._stereo_unavailable = 0
        self._refinement_distances_px: list[float] = []

    def statistics(self) -> dict[str, object]:
        with self._lock:
            distances = tuple(self._refinement_distances_px)
            return {
                "stereo_enabled": self._stereo_extractor is not None,
                "stereo_attempts": self._stereo_attempts,
                "stereo_selected": self._stereo_selected,
                "stereo_unavailable": self._stereo_unavailable,
                "refinement_distance_px": _sample_summary(distances),
            }

    def delivery_succeeded(self, payloads: tuple[CropPayload, ...]) -> None:
        """Commit crop reservations after inference acknowledges the batch."""

        with self._lock:
            for payload in payloads:
                self._pending.pop(payload.job.job_id, None)

    def delivery_failed(self, payloads: tuple[CropPayload, ...]) -> None:
        """Release failed reservations so a later frame can replace the sample."""

        with self._lock:
            for payload in payloads:
                reserved = self._pending.pop(payload.job.job_id, None)
                if reserved is None:
                    continue
                bean_ref, sample_index = reserved
                self._active_counts[bean_ref] = max(
                    0, self._active_counts.get(bean_ref, 1) - 1
                )
                retry = self._retry_indices.setdefault(bean_ref, [])
                if sample_index not in retry:
                    heapq.heappush(retry, sample_index)

    def select(
        self,
        frame_bgr: Any,
        analysis: FrameAnalysis,
        revisions: dict[BeanRef, int],
    ) -> tuple[CropPayload, ...]:
        selected: list[CropPayload] = []
        for track in analysis.tracks:
            if track.status not in {TrackStatus.TENTATIVE, TrackStatus.CONFIRMED}:
                continue
            if not track.history:
                continue
            observation = track.history[-1]
            if observation.frame_index != analysis.frame_index:
                continue
            with self._lock:
                active_count = self._active_counts.get(track.bean_ref, 0)
                if active_count >= self.settings.max_crops_per_bean:
                    continue
            frame_size = _frame_size_px(frame_bgr)
            if _bbox_touches_frame_edge(
                observation.detection.bbox_px,
                frame_size,
            ):
                # The classifier must never see an actually truncated bean.
                continue
            source_size = self.settings.size_px
            resized = False
            materializer = None
            camr_materializer = None
            stereo_pair = None
            prepared = self._prepare(
                frame_bgr,
                observation.detection.centroid_px,
                source_size,
            )
            if (
                prepared is not None
                and self._stereo_extractor is not None
                and prepared.source_size_px != source_size
            ):
                source_size = prepared.source_size_px
                resized = True
            if (
                prepared is None
                and self.settings.adaptive_edge_resize
                and self._stereo_extractor is None
            ):
                # The stereo extractor already tries the largest complete size
                # shared by both sensors. Repeating it with CamL-only edge
                # geometry cannot recover a clipped CamR bean and needlessly
                # repeats homography projection and local segmentation.
                source_size = _largest_complete_centred_crop(
                    observation.detection.centroid_px,
                    observation.detection.bbox_px,
                    frame_size,
                    self.settings.size_px,
                )
                if source_size is not None:
                    prepared = self._prepare(
                        frame_bgr,
                        observation.detection.centroid_px,
                        source_size,
                    )
                    if prepared is not None and self._stereo_extractor is not None:
                        source_size = prepared.source_size_px
                    resized = prepared is not None and source_size != self.settings.size_px
            if prepared is None:
                continue
            if self._stereo_extractor is not None:
                materializer = prepared.caml_materializer
                camr_materializer = prepared.camr_materializer
                padded = prepared.padded
                stereo_pair = prepared.pair
                if resized:
                    materializer = _resized_materializer(
                        materializer,
                        self.settings.size_px,
                    )
                    camr_materializer = _resized_materializer(
                        camr_materializer,
                        self.settings.size_px,
                    )
                crop_width = crop_height = self.settings.size_px
                crop = None
                with self._lock:
                    self._stereo_selected += 1
                    self._refinement_distances_px.append(
                        stereo_pair.refinement_distance_px
                    )
            elif self._deferred_extractor is not None:
                materializer, _crop_width, _crop_height, padded = prepared
                if resized:
                    materializer = _resized_materializer(
                        materializer,
                        self.settings.size_px,
                    )
                crop_width = crop_height = self.settings.size_px
                crop = None
            else:
                crop, padded = prepared
                if resized:
                    crop = cv2.resize(
                        crop,
                        (self.settings.size_px, self.settings.size_px),
                        interpolation=cv2.INTER_LINEAR,
                    )
                crop_width, crop_height = crop.shape[1], crop.shape[0]
            revision = revisions.get(track.bean_ref)
            if revision is None:
                continue
            with self._lock:
                retry = self._retry_indices.get(track.bean_ref)
                if retry:
                    sample_index = heapq.heappop(retry)
                else:
                    sample_index = self._next_indices.get(track.bean_ref, 0)
                    self._next_indices[track.bean_ref] = sample_index + 1
            job_id = ":".join(
                (
                    "infer",
                    track.bean_ref.run_id,
                    str(track.bean_ref.sequence),
                    self.settings.camera_id,
                    str(observation.frame_index),
                    str(sample_index),
                )
            )
            job = InferenceJob(
                job_id=job_id,
                bean_ref=track.bean_ref,
                status=InferenceStatus.SUBMITTED,
                camera_id=self.settings.camera_id,
                frame_index=observation.frame_index,
                capture_timestamp_ns=observation.timestamp_ns,
                source_registry_revision=revision,
                crop_width_px=crop_width,
                crop_height_px=crop_height,
                padded=padded,
                submitted_timestamp_ns=observation.timestamp_ns,
                updated_timestamp_ns=observation.timestamp_ns,
                source_crop_width_px=source_size,
                source_crop_height_px=source_size,
                resized=resized,
                timing_marks_ns={
                    "first_detection_source_ns": track.history[0].timestamp_ns,
                    "first_fully_visible_source_ns": observation.timestamp_ns,
                    "crop_capture_source_ns": observation.timestamp_ns,
                    "crop_selected_monotonic_ns": time.monotonic_ns(),
                    "expected_inference_samples": (
                        self.settings.max_crops_per_bean
                    ),
                },
            )
            with self._lock:
                self._active_counts[track.bean_ref] = (
                    self._active_counts.get(track.bean_ref, 0) + 1
                )
                self._pending[job_id] = (track.bean_ref, sample_index)
            selected.append(
                CropPayload(
                    job,
                    crop,
                    materializer,
                    None,
                    camr_materializer,
                    stereo_pair,
                )
            )
        return tuple(selected)

    def _prepare(
        self,
        frame_bgr: Any,
        centroid_px: tuple[float, float],
        source_size: int,
    ):
        if self._stereo_extractor is not None:
            with self._lock:
                self._stereo_attempts += 1
            result = self._stereo_extractor(
                frame_bgr,
                centroid_px,
                source_size,
                allow_padding=self.settings.allow_padding,
                allow_resize=self.settings.adaptive_edge_resize,
            )
            if result is None:
                with self._lock:
                    self._stereo_unavailable += 1
            return result
        if self._deferred_extractor is not None:
            return self._deferred_extractor(
                frame_bgr,
                centroid_px,
                source_size,
                allow_padding=self.settings.allow_padding,
            )
        crop, padded = self._extractor(
            frame_bgr,
            centroid_px,
            source_size,
            allow_padding=self.settings.allow_padding,
        )
        return None if crop is None else (crop, padded)


def _frame_size_px(frame: Any) -> tuple[int, int]:
    native = getattr(frame, "native_size_px", None)
    if native is not None:
        return int(native[0]), int(native[1])
    if not hasattr(frame, "shape") or len(frame.shape) < 2:
        raise ValueError("inference crop source has no image dimensions")
    return int(frame.shape[1]), int(frame.shape[0])


def _bbox_touches_frame_edge(
    bbox_px: tuple[int, int, int, int], frame_size_px: tuple[int, int]
) -> bool:
    x, y, width, height = bbox_px
    frame_width, frame_height = frame_size_px
    return (
        x <= 0
        or y <= 0
        or x + width >= frame_width
        or y + height >= frame_height
    )


def _largest_complete_centred_crop(
    centroid_px: tuple[float, float],
    bbox_px: tuple[int, int, int, int],
    frame_size_px: tuple[int, int],
    target_size_px: int,
) -> int | None:
    centre_x, centre_y = centroid_px
    frame_width, frame_height = frame_size_px
    radius = math.floor(
        min(centre_x, centre_y, frame_width - centre_x, frame_height - centre_y)
    )
    available = min(target_size_px, max(0, radius * 2))
    if available % 2:
        available -= 1
    x, y, width, height = bbox_px
    required_radius = max(
        centre_x - x,
        x + width - centre_x,
        centre_y - y,
        y + height - centre_y,
    )
    required = math.ceil(required_radius * 2)
    return available if available >= max(2, required) else None


def _resized_materializer(
    materializer: Callable[[], np.ndarray], target_size_px: int
) -> Callable[[], np.ndarray]:
    def resized() -> np.ndarray:
        return cv2.resize(
            materializer(),
            (target_size_px, target_size_px),
            interpolation=cv2.INTER_LINEAR,
        )

    return resized


def _validate_materialized_image(image: np.ndarray, camera: str) -> None:
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError(
            f"materialized {camera} inference crop must be an 8-bit BGR image"
        )


def _sample_summary(values: tuple[float, ...]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "mean": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "max": max(values),
    }


def extract_square_crop(
    frame_bgr: np.ndarray,
    centroid_px: tuple[float, float],
    size_px: int,
    *,
    allow_padding: bool,
) -> tuple[np.ndarray | None, bool]:
    if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
        raise ValueError("inference crop source must be a BGR image")
    if size_px <= 0:
        raise ValueError("crop size must be positive")
    centre_x = round(centroid_px[0])
    centre_y = round(centroid_px[1])
    left = centre_x - size_px // 2
    top = centre_y - size_px // 2
    right = left + size_px
    bottom = top + size_px
    height, width = frame_bgr.shape[:2]
    complete = left >= 0 and top >= 0 and right <= width and bottom <= height
    if complete:
        return np.ascontiguousarray(frame_bgr[top:bottom, left:right]), False
    if not allow_padding:
        return None, False
    output = np.zeros((size_px, size_px, 3), dtype=frame_bgr.dtype)
    source_left = max(0, left)
    source_top = max(0, top)
    source_right = min(width, right)
    source_bottom = min(height, bottom)
    if source_left >= source_right or source_top >= source_bottom:
        return None, False
    destination_left = source_left - left
    destination_top = source_top - top
    output[
        destination_top : destination_top + (source_bottom - source_top),
        destination_left : destination_left + (source_right - source_left),
    ] = frame_bgr[source_top:source_bottom, source_left:source_right]
    return output, True
