"""Sequential composition of detection, tracking and sorting prediction."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .background import BackgroundProvenance
from .calibration import MetricPlaneCalibration
from .detection import BeanDetector
from .events import EventBus
from .models import FrameAnalysis, Observation
from .prediction import GateLayout, TrajectoryPredictor
from .source import RecordingVideoSource
from .tracking import TrackManager, TrackerSettings


ProgressCallback = Callable[[int, int, FrameAnalysis], None]


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    run_id: str
    source_path: Path
    exact_timestamps: bool
    frames: tuple[FrameAnalysis, ...]
    background: BackgroundProvenance = BackgroundProvenance("unspecified", ())

    @property
    def mean_processing_ms(self) -> float:
        if not self.frames:
            return 0.0
        return sum(frame.processing_ms for frame in self.frames) / len(self.frames)

    @property
    def p95_processing_ms(self) -> float:
        if not self.frames:
            return 0.0
        ordered = sorted(frame.processing_ms for frame in self.frames)
        return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))]


class AnalysisEngine:
    def __init__(
        self,
        calibration: MetricPlaneCalibration,
        detector: BeanDetector,
        background_bgr,
        *,
        tracker_settings: TrackerSettings | None = None,
        gate_layout: GateLayout | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.calibration = calibration
        self.detector = detector
        self.background_bgr = background_bgr
        self.tracker_settings = tracker_settings or TrackerSettings()
        self.gate_layout = gate_layout or GateLayout(calibration.sorting_line_y())
        self.events = events
        self.tracker = TrackManager(
            top_y_mm=calibration.top_y_mm,
            bottom_y_mm=calibration.bottom_y_mm,
            image_width_px=calibration.image_size_px[0],
            settings=self.tracker_settings,
            events=events,
        )
        self.predictor = TrajectoryPredictor(
            self.gate_layout,
            gravity_mm_s2=self.tracker_settings.gravity_mm_s2,
            process_acceleration_sigma_mm_s2=(
                self.tracker_settings.process_acceleration_sigma_mm_s2
            ),
        )

    def process(self, frame_bgr, frame_index: int, timestamp_ns: int) -> FrameAnalysis:
        started = time.perf_counter_ns()
        detection_result = self.detector.detect(frame_bgr, self.background_bgr)
        observations = tuple(
            Observation(
                frame_index=frame_index,
                timestamp_ns=timestamp_ns,
                detection=detection,
                position_mm=self.calibration.pixel_to_mm(detection.centroid_px),
            )
            for detection in detection_result.detections
        )
        tracks = self.tracker.update(observations, timestamp_ns)
        predictions = tuple(
            prediction
            for track in tracks
            if (prediction := self.predictor.predict(track)) is not None
        )
        processing_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        return FrameAnalysis(
            frame_index=frame_index,
            timestamp_ns=timestamp_ns,
            detections=detection_result.detections,
            rejections=self.tracker.last_rejections,
            tracks=tracks,
            predictions=predictions,
            processing_ms=processing_ms,
        )


def analyse_source(
    source: RecordingVideoSource,
    engine: AnalysisEngine,
    *,
    stop: Event | None = None,
    progress: ProgressCallback | None = None,
    background_provenance: BackgroundProvenance | None = None,
) -> AnalysisRun:
    cancellation = stop or Event()
    frames: list[FrameAnalysis] = []
    total = source.metadata.frame_count
    for index in range(total):
        if cancellation.is_set():
            break
        frame = source.frame(index)
        analysis = engine.process(frame, index, source.timestamp_ns(index))
        frames.append(analysis)
        if progress is not None:
            progress(index + 1, total, analysis)
    return AnalysisRun(
        run_id=engine.tracker.run_id,
        source_path=source.path,
        exact_timestamps=source.metadata.exact_timestamps,
        frames=tuple(frames),
        background=background_provenance or BackgroundProvenance("unspecified", ()),
    )


def export_run_json(run: AnalysisRun, path: Path) -> None:
    """Export compact track/prediction data without any frame images."""

    payload = {
        "schema": "beanoflight-analysis/v1",
        "run_id": run.run_id,
        "source_path": str(run.source_path),
        "exact_timestamps": run.exact_timestamps,
        "background": {
            "method": run.background.method,
            "frame_indices": list(run.background.frame_indices),
            "candidate_seed": run.background.candidate_seed,
        },
        "performance": {
            "mean_processing_ms": run.mean_processing_ms,
            "p95_processing_ms": run.p95_processing_ms,
        },
        "frames": [_frame_json(frame) for frame in run.frames],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _frame_json(frame: FrameAnalysis) -> dict[str, object]:
    return {
        "frame_index": frame.frame_index,
        "timestamp_ns": frame.timestamp_ns,
        "processing_ms": frame.processing_ms,
        "rejections": [
            {
                "reason": rejection.reason,
                "centroid_px": list(rejection.observation.detection.centroid_px),
                "bbox_px": list(rejection.observation.detection.bbox_px),
            }
            for rejection in frame.rejections
        ],
        "tracks": [
            {
                "bean_id": str(track.bean_ref),
                "status": track.status.value,
                "state": list(track.state),
                "hits": track.hits,
                "misses": track.misses,
            }
            for track in frame.tracks
        ],
        "predictions": [
            {
                "bean_id": str(prediction.bean_ref),
                "line_y_mm": prediction.line_y_mm,
                "crossing_timestamp_ns": prediction.crossing_timestamp_ns,
                "x_mean_mm": prediction.x_mean_mm,
                "x_std_mm": prediction.x_std_mm,
                "time_std_ms": prediction.time_std_ms,
                "selected_gate_indices": list(prediction.selected_gate_indices),
                "gate_probabilities": {
                    item.gate.label: item.probability for item in prediction.gates
                },
            }
            for prediction in frame.predictions
        ],
    }
