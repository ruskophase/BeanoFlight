"""Bounded, deterministic selection of lossless inference crops."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from .models import BeanRef, FrameAnalysis, TrackStatus
from .registry_models import InferenceJob, InferenceStatus


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

    def materialized(self) -> CropPayload:
        if self.image_bgr is not None:
            return self
        if self.materializer is None:
            raise ValueError("crop payload has neither an image nor a materializer")
        image = self.materializer()
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError("materialized inference crop must be an 8-bit BGR image")
        return CropPayload(self.job, image)


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
    ) -> None:
        self.settings = settings or CropSettings()
        self.settings.validate()
        self._extractor = extractor or extract_square_crop
        self._deferred_extractor = deferred_extractor
        self._counts: dict[BeanRef, int] = {}

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
            count = self._counts.get(track.bean_ref, 0)
            if count >= self.settings.max_crops_per_bean:
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
            prepared = self._prepare(
                frame_bgr,
                observation.detection.centroid_px,
                source_size,
            )
            if prepared is None and self.settings.adaptive_edge_resize:
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
                    resized = prepared is not None and source_size != self.settings.size_px
            if prepared is None:
                continue
            if self._deferred_extractor is not None:
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
            job_id = ":".join(
                (
                    "infer",
                    track.bean_ref.run_id,
                    str(track.bean_ref.sequence),
                    self.settings.camera_id,
                    str(observation.frame_index),
                    str(count),
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
            self._counts[track.bean_ref] = count + 1
            selected.append(CropPayload(job, crop, materializer))
        return tuple(selected)

    def _prepare(
        self,
        frame_bgr: Any,
        centroid_px: tuple[float, float],
        source_size: int,
    ):
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
