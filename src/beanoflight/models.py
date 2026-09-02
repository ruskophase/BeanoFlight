"""Small immutable records passed between BeanoFlight pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np


class TrackStatus(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    OCCLUDED = "occluded"
    EXITED = "exited"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, order=True)
class BeanRef:
    run_id: str
    sequence: int

    def __str__(self) -> str:
        return f"{self.run_id[:8]}/{self.sequence:06d}"


@dataclass(frozen=True, slots=True)
class Detection:
    centroid_px: tuple[float, float]
    bbox_px: tuple[int, int, int, int]
    area_px: int
    solidity: float
    mean_bgr: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class Observation:
    frame_index: int
    timestamp_ns: int
    detection: Detection
    position_mm: tuple[float, float]


@dataclass(frozen=True, slots=True)
class DetectionRejection:
    observation: Observation
    reason: str


@dataclass(frozen=True, slots=True)
class PipelineStage:
    key: str
    name: str
    image: np.ndarray = field(compare=False, repr=False)
    settings: tuple[str, ...] = ()
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class TrackSnapshot:
    bean_ref: BeanRef
    status: TrackStatus
    timestamp_ns: int
    state: tuple[float, float, float, float]
    covariance: tuple[tuple[float, ...], ...]
    hits: int
    misses: int
    last_bbox_px: tuple[int, int, int, int]
    history: tuple[Observation, ...]

    @property
    def x_mm(self) -> float:
        return self.state[0]

    @property
    def y_mm(self) -> float:
        return self.state[1]


@dataclass(frozen=True, slots=True)
class Gate:
    index: int
    left_mm: float
    right_mm: float

    @property
    def label(self) -> str:
        return f"G{self.index:+d}" if self.index else "G0"


@dataclass(frozen=True, slots=True)
class GateProbability:
    gate: Gate
    probability: float


@dataclass(frozen=True, slots=True)
class CrossingPrediction:
    bean_ref: BeanRef
    line_y_mm: float
    crossing_timestamp_ns: int
    seconds_until_crossing: float
    x_mean_mm: float
    x_std_mm: float
    time_std_ms: float
    gates: tuple[GateProbability, ...]
    selected_gate_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AnalysisTimings:
    detection_ms: float
    coordinate_mapping_ms: float
    tracking_ms: float
    prediction_ms: float
    registry_ms: float

    def as_dict(self) -> dict[str, float]:
        return {
            "detection_ms": self.detection_ms,
            "coordinate_mapping_ms": self.coordinate_mapping_ms,
            "tracking_ms": self.tracking_ms,
            "prediction_ms": self.prediction_ms,
            "registry_ms": self.registry_ms,
        }


@dataclass(frozen=True, slots=True)
class FrameAnalysis:
    frame_index: int
    timestamp_ns: int
    detections: tuple[Detection, ...]
    rejections: tuple[DetectionRejection, ...]
    tracks: tuple[TrackSnapshot, ...]
    predictions: tuple[CrossingPrediction, ...]
    processing_ms: float
    timings: AnalysisTimings | None = None


@dataclass(frozen=True, slots=True)
class BeanEvent:
    kind: str
    bean_ref: BeanRef
    timestamp_ns: int
    payload: dict[str, Any] = field(default_factory=dict)
    revision: int = 0
    event_id: str = ""
    stream_sequence: int = 0
