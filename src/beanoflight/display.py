"""Pure OpenCV renderers for inspector captions and flight overlays."""

from __future__ import annotations

import math

import cv2
import numpy as np

from .calibration import CalibrationError, MetricPlaneCalibration
from .models import CrossingPrediction, FrameAnalysis, PipelineStage, TrackSnapshot, TrackStatus
from .prediction import GateLayout


STATUS_COLOURS = {
    TrackStatus.TENTATIVE: (30, 200, 255),
    TrackStatus.CONFIRMED: (90, 255, 120),
    TrackStatus.OCCLUDED: (255, 180, 60),
    TrackStatus.EXITED: (180, 180, 180),
    TrackStatus.CANCELLED: (80, 80, 210),
}


def render_pipeline_stage(stage: PipelineStage) -> np.ndarray:
    if stage.image.ndim == 2:
        rendered = cv2.cvtColor(stage.image, cv2.COLOR_GRAY2BGR)
    else:
        rendered = stage.image.copy()
    lines = (stage.name, *stage.settings)
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.42, min(rendered.shape[:2]) / 1_800.0)
    line_height = max(18, round(font_scale * 30))
    panel_height = 16 + line_height * len(lines)
    panel_width = min(
        rendered.shape[1],
        max(430, max((len(line) for line in lines), default=1) * round(font_scale * 14)),
    )
    overlay = rendered.copy()
    cv2.rectangle(overlay, (0, 0), (panel_width, panel_height), (5, 8, 12), -1)
    cv2.addWeighted(overlay, 0.78, rendered, 0.22, 0.0, rendered)
    for index, line in enumerate(lines):
        colour = (90, 245, 255) if index == 0 else (230, 235, 240)
        thickness = 2 if index == 0 else 1
        cv2.putText(
            rendered,
            line,
            (10, 9 + line_height * (index + 1)),
            font,
            font_scale,
            colour,
            thickness,
            cv2.LINE_AA,
        )
    return rendered


def render_analysis(
    frame_bgr: np.ndarray,
    analysis: FrameAnalysis,
    calibration: MetricPlaneCalibration,
    layout: GateLayout,
    *,
    gravity_mm_s2: float = 9_810.0,
    left_birth_margin_px: int = 0,
    right_birth_margin_px: int = 0,
) -> np.ndarray:
    height, width = frame_bgr.shape[:2]
    line_centre = calibration.mm_to_pixel((0.0, layout.line_y_mm))
    output_height = max(height + 80, int(math.ceil(line_centre[1] + 80)))
    output_height = min(max(output_height, height), height * 2)
    rendered = np.full((output_height, width, 3), (13, 16, 20), dtype=np.uint8)
    rendered[:height] = frame_bgr
    draw_birth_margins(
        rendered[:height], left_birth_margin_px, right_birth_margin_px
    )
    if output_height > height:
        cv2.line(rendered, (0, height), (width - 1, height), (90, 100, 112), 1)
        cv2.putText(
            rendered,
            "VIRTUAL REGION BELOW PHYSICAL FoV",
            (12, height + 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (165, 175, 185),
            1,
            cv2.LINE_AA,
        )
    prediction_by_id = {prediction.bean_ref: prediction for prediction in analysis.predictions}
    _draw_gates(rendered, calibration, layout, tuple(analysis.predictions))
    assigned_centres = {
        track.history[-1].detection.centroid_px
        for track in analysis.tracks
        if track.history and track.history[-1].frame_index == analysis.frame_index
    }
    rejected_centres = {
        rejection.observation.detection.centroid_px for rejection in analysis.rejections
    }
    for rejection in analysis.rejections:
        detection = rejection.observation.detection
        x, y, component_width, component_height = detection.bbox_px
        rejection_label = (
            "EDGE-REJECTED" if "margin" in rejection.reason else "BIRTH-REJECTED"
        )
        cv2.rectangle(
            rendered,
            (x, y),
            (x + component_width - 1, y + component_height - 1),
            (45, 55, 245),
            3,
        )
        cv2.putText(
            rendered,
            f"{rejection_label}: {rejection.reason.upper()}",
            (
                x + 4,
                y - 7 if y >= 45 else min(rendered.shape[0] - 10, y + component_height + 20),
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.43,
            (45, 55, 245),
            2,
            cv2.LINE_AA,
        )
    for detection in analysis.detections:
        if (
            detection.centroid_px in assigned_centres
            or detection.centroid_px in rejected_centres
        ):
            continue
        x, y, component_width, component_height = detection.bbox_px
        cv2.rectangle(
            rendered,
            (x, y),
            (x + component_width - 1, y + component_height - 1),
            (210, 90, 210),
            2,
        )
        cv2.putText(
            rendered,
            "UNASSIGNED",
            (x + 4, max(14, y - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.40,
            (210, 90, 210),
            1,
            cv2.LINE_AA,
        )
    for track in analysis.tracks:
        _draw_track(
            rendered,
            track,
            prediction_by_id.get(track.bean_ref),
            calibration,
            gravity_mm_s2,
        )
    cv2.putText(
        rendered,
        f"frame {analysis.frame_index + 1}  timestamp {analysis.timestamp_ns} ns  "
        f"processing {analysis.processing_ms:.2f} ms",
        (10, height - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.44,
        (235, 235, 235),
        1,
        cv2.LINE_AA,
    )
    return rendered


def draw_birth_margins(
    image_bgr: np.ndarray, left_margin_px: int, right_margin_px: int
) -> np.ndarray:
    """Shade display-only lateral regions where new tracks cannot be born."""

    height, width = image_bgr.shape[:2]
    left = max(0, min(width, int(left_margin_px)))
    right = max(0, min(width - left, int(right_margin_px)))
    if left <= 0 and right <= 0:
        return image_bgr
    overlay = image_bgr.copy()
    if left > 0:
        cv2.rectangle(overlay, (0, 0), (left - 1, height - 1), (20, 35, 150), -1)
        cv2.line(image_bgr, (left, 0), (left, height - 1), (40, 70, 255), 2)
    if right > 0:
        boundary = width - right
        cv2.rectangle(
            overlay, (boundary, 0), (width - 1, height - 1), (20, 35, 150), -1
        )
        cv2.line(
            image_bgr, (boundary, 0), (boundary, height - 1), (40, 70, 255), 2
        )
    cv2.addWeighted(overlay, 0.32, image_bgr, 0.68, 0.0, image_bgr)
    label_y = min(height - 8, 24)
    if left > 0:
        cv2.putText(
            image_bgr,
            f"NO BIRTH {left}px",
            (5, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (110, 130, 255),
            1,
            cv2.LINE_AA,
        )
    if right > 0:
        label = f"NO BIRTH {right}px"
        (text_width, _text_height), _baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1
        )
        cv2.putText(
            image_bgr,
            label,
            (max(5, width - text_width - 5), label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (110, 130, 255),
            1,
            cv2.LINE_AA,
        )
    return image_bgr


def _draw_track(
    image: np.ndarray,
    track: TrackSnapshot,
    prediction: CrossingPrediction | None,
    calibration: MetricPlaneCalibration,
    gravity: float,
) -> None:
    colour = STATUS_COLOURS[track.status]
    x, y, width, height = track.last_bbox_px
    cv2.rectangle(image, (x, y), (x + width - 1, y + height - 1), colour, 2)
    measured = [
        (round(item.detection.centroid_px[0]), round(item.detection.centroid_px[1]))
        for item in track.history
    ]
    if len(measured) >= 2:
        cv2.polylines(image, [np.asarray(measured, np.int32)], False, colour, 2, cv2.LINE_AA)
    if measured:
        cv2.circle(image, measured[-1], 4, (30, 255, 255), -1, cv2.LINE_AA)
        cv2.putText(
            image,
            f"{track.bean_ref.sequence:06d} {track.status.value}",
            (measured[-1][0] + 8, measured[-1][1] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            colour,
            2,
            cv2.LINE_AA,
        )
    if prediction is None:
        return
    state_x, state_y, vx, vy = track.state
    points: list[tuple[int, int]] = []
    for fraction in np.linspace(0.0, 1.0, 25):
        dt = prediction.seconds_until_crossing * float(fraction)
        point_mm = (
            state_x + vx * dt,
            state_y + vy * dt + 0.5 * gravity * dt * dt,
        )
        try:
            point_px = calibration.mm_to_pixel(point_mm)
        except CalibrationError:
            continue
        points.append((round(point_px[0]), round(point_px[1])))
    _draw_dashed_polyline(image, points, colour)
    mean_point = calibration.mm_to_pixel((prediction.x_mean_mm, prediction.line_y_mm))
    low_point = calibration.mm_to_pixel(
        (prediction.x_mean_mm - 1.96 * prediction.x_std_mm, prediction.line_y_mm)
    )
    high_point = calibration.mm_to_pixel(
        (prediction.x_mean_mm + 1.96 * prediction.x_std_mm, prediction.line_y_mm)
    )
    cv2.line(
        image,
        (round(low_point[0]), round(low_point[1])),
        (round(high_point[0]), round(high_point[1])),
        colour,
        5,
        cv2.LINE_AA,
    )
    best = max(prediction.gates, key=lambda item: item.probability)
    cv2.putText(
        image,
        f"{track.bean_ref.sequence:06d} {best.gate.label} {best.probability:.0%} "
        f"in {prediction.seconds_until_crossing * 1000:.1f}ms",
        (round(mean_point[0]) + 8, round(mean_point[1]) - 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.43,
        colour,
        1,
        cv2.LINE_AA,
    )


def _draw_gates(
    image: np.ndarray,
    calibration: MetricPlaneCalibration,
    layout: GateLayout,
    predictions: tuple[CrossingPrediction, ...],
) -> None:
    selected = {
        gate_index
        for prediction in predictions
        for gate_index in prediction.selected_gate_indices
    }
    for gate in layout.gates:
        left = calibration.mm_to_pixel((gate.left_mm, layout.line_y_mm))
        right = calibration.mm_to_pixel((gate.right_mm, layout.line_y_mm))
        y = round((left[1] + right[1]) * 0.5)
        x1, x2 = sorted((round(left[0]), round(right[0])))
        colour = (35, 95, 190) if gate.index in selected else (65, 72, 82)
        cv2.rectangle(image, (x1, y - 8), (x2, y + 8), colour, -1)
        cv2.rectangle(image, (x1, y - 8), (x2, y + 8), (180, 190, 205), 1)
        if gate.index % 2 == 0 and 0 <= x1 < image.shape[1]:
            cv2.putText(
                image,
                gate.label,
                (x1 + 2, y + 27),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.32,
                (175, 185, 200),
                1,
                cv2.LINE_AA,
            )
    line_left = calibration.mm_to_pixel((layout.gates[0].left_mm, layout.line_y_mm))
    line_right = calibration.mm_to_pixel((layout.gates[-1].right_mm, layout.line_y_mm))
    cv2.line(
        image,
        (round(line_left[0]), round(line_left[1])),
        (round(line_right[0]), round(line_right[1])),
        (220, 220, 230),
        2,
        cv2.LINE_AA,
    )


def _draw_dashed_polyline(
    image: np.ndarray, points: list[tuple[int, int]], colour: tuple[int, int, int]
) -> None:
    for index, (first, second) in enumerate(zip(points, points[1:])):
        if index % 2 == 0:
            cv2.line(image, first, second, colour, 2, cv2.LINE_AA)
