"""Bounded, deterministic selection of lossless inference crops."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .models import BeanRef, FrameAnalysis, TrackStatus
from .registry_models import InferenceJob, InferenceStatus


@dataclass(frozen=True, slots=True)
class CropSettings:
    size_px: int = 300
    camera_id: str = "CamL"
    allow_padding: bool = False
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
    image_bgr: np.ndarray = field(compare=False, repr=False)


class BeanCropSelector:
    """Choose the first fully visible confirmed observation for each bean."""

    def __init__(self, settings: CropSettings | None = None) -> None:
        self.settings = settings or CropSettings()
        self.settings.validate()
        self._counts: dict[BeanRef, int] = {}

    def select(
        self,
        frame_bgr: np.ndarray,
        analysis: FrameAnalysis,
        revisions: dict[BeanRef, int],
    ) -> tuple[CropPayload, ...]:
        selected: list[CropPayload] = []
        for track in analysis.tracks:
            if track.status != TrackStatus.CONFIRMED or not track.history:
                continue
            observation = track.history[-1]
            if observation.frame_index != analysis.frame_index:
                continue
            count = self._counts.get(track.bean_ref, 0)
            if count >= self.settings.max_crops_per_bean:
                continue
            crop, padded = extract_square_crop(
                frame_bgr,
                observation.detection.centroid_px,
                self.settings.size_px,
                allow_padding=self.settings.allow_padding,
            )
            if crop is None:
                continue
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
                crop_width_px=crop.shape[1],
                crop_height_px=crop.shape[0],
                padded=padded,
                submitted_timestamp_ns=observation.timestamp_ns,
                updated_timestamp_ns=observation.timestamp_ns,
            )
            self._counts[track.bean_ref] = count + 1
            selected.append(CropPayload(job, crop))
        return tuple(selected)


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
