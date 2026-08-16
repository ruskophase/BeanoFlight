"""PinkPlane correspondence conversion into a CamL metric-plane mapping."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

METRIC_SCHEMA = "beanoflight-metric-plane/v1"
PINKPLANE_SCHEMAS = {"pinkplane-homography/v2"}


class CalibrationError(ValueError):
    pass


def _finite_matrix(value: Any, shape: tuple[int, int]) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise CalibrationError("calibration matrix must be numeric") from exc
    if matrix.shape != shape or not np.all(np.isfinite(matrix)):
        raise CalibrationError(f"calibration matrix must be finite with shape {shape}")
    return matrix


def _normalise_homography(matrix: np.ndarray) -> np.ndarray:
    if abs(float(matrix[2, 2])) < 1e-12:
        raise CalibrationError("calibration homography is singular")
    matrix = matrix / matrix[2, 2]
    if abs(float(np.linalg.det(matrix))) < 1e-12:
        raise CalibrationError("calibration homography is singular")
    return matrix


@dataclass(frozen=True, slots=True)
class MetricPlaneCalibration:
    source_path: Path
    source_sha256: str
    image_size_px: tuple[int, int]
    hole_pitch_mm: float
    pixel_to_mm_matrix: np.ndarray
    mm_to_pixel_matrix: np.ndarray
    rms_error_mm: float
    max_error_mm: float
    top_y_mm: float
    bottom_y_mm: float

    def pixel_to_mm(self, point: tuple[float, float]) -> tuple[float, float]:
        return _project(self.pixel_to_mm_matrix, point)

    def mm_to_pixel(self, point: tuple[float, float]) -> tuple[float, float]:
        return _project(self.mm_to_pixel_matrix, point)

    def pixels_to_mm(
        self, points: Iterable[tuple[float, float]]
    ) -> tuple[tuple[float, float], ...]:
        values = np.asarray(tuple(points), dtype=np.float64)
        if values.size == 0:
            return ()
        mapped = cv2.perspectiveTransform(
            values.reshape(-1, 1, 2), self.pixel_to_mm_matrix
        )
        return tuple((float(x), float(y)) for x, y in mapped.reshape(-1, 2))

    def measurement_covariance(
        self, point_px: tuple[float, float], sigma_px: float = 1.5
    ) -> np.ndarray:
        """Propagate isotropic centroid uncertainty through the local homography."""

        x, y = point_px
        epsilon = 0.5
        centre = np.asarray(self.pixel_to_mm((x, y)))
        dx = (np.asarray(self.pixel_to_mm((x + epsilon, y))) - centre) / epsilon
        dy = (np.asarray(self.pixel_to_mm((x, y + epsilon))) - centre) / epsilon
        jacobian = np.column_stack((dx, dy))
        covariance = jacobian @ (np.eye(2) * sigma_px**2) @ jacobian.T
        residual_variance = max(self.rms_error_mm, 0.01) ** 2
        return covariance + np.eye(2) * residual_variance

    def sorting_line_y(self, offset_below_fov_mm: float = 30.0) -> float:
        if offset_below_fov_mm <= 0:
            raise CalibrationError("sorting-line offset must be positive")
        return self.bottom_y_mm + offset_below_fov_mm

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": METRIC_SCHEMA,
            "source": {
                "path": str(self.source_path),
                "sha256": self.source_sha256,
                "coordinate_domain": "undistorted",
            },
            "image_size_px": list(self.image_size_px),
            "hole_pitch_mm": self.hole_pitch_mm,
            "coordinates": {
                "origin": "centre of the undistorted CamL image",
                "positive_x": "right",
                "positive_y": "down",
            },
            "mapping": {
                "direction": "CamL undistorted pixels to fall-plane millimetres",
                "matrix": self.pixel_to_mm_matrix.tolist(),
                "inverse_matrix": self.mm_to_pixel_matrix.tolist(),
            },
            "quality": {
                "rms_fit_error_mm": self.rms_error_mm,
                "max_fit_error_mm": self.max_error_mm,
            },
            "fov": {
                "top_centre_y_mm": self.top_y_mm,
                "bottom_centre_y_mm": self.bottom_y_mm,
            },
        }

    @classmethod
    def from_pinkplane(
        cls,
        path: Path,
        *,
        image_size_px: tuple[int, int] = (1456, 1088),
        hole_pitch_mm: float = 9.16,
    ) -> MetricPlaneCalibration:
        path = path.expanduser().resolve()
        if not math.isfinite(hole_pitch_mm) or hole_pitch_mm <= 0:
            raise CalibrationError("hole pitch must be positive")
        try:
            encoded = path.read_bytes()
            payload = json.loads(encoded)
        except (OSError, json.JSONDecodeError) as exc:
            raise CalibrationError(f"cannot read PinkPlane calibration: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") not in PINKPLANE_SCHEMAS
        ):
            raise CalibrationError(
                "metric conversion requires a PinkPlane v2 homography"
            )
        mapping = payload.get("mapping", {})
        if mapping.get("coordinate_domain") != "undistorted":
            raise CalibrationError("PinkPlane coordinates must be undistorted")
        correspondence = payload.get("correspondence", {})
        row_counts = correspondence.get("row_counts")
        points_value = correspondence.get("mean_CamL_points_px")
        if not isinstance(row_counts, list) or not row_counts:
            raise CalibrationError("PinkPlane file has no row layout")
        if not all(isinstance(count, int) and count > 0 for count in row_counts):
            raise CalibrationError("PinkPlane row counts are invalid")
        points = _finite_matrix(points_value, (sum(row_counts), 2))
        if len(points) < 4:
            raise CalibrationError("at least four PinkPlane points are required")

        grid = np.asarray(
            [
                (column * hole_pitch_mm, row * hole_pitch_mm)
                for row, count in enumerate(row_counts)
                for column in range(count)
            ],
            dtype=np.float64,
        )
        raw_matrix, _mask = cv2.findHomography(points, grid, method=0)
        if raw_matrix is None:
            raise CalibrationError("OpenCV could not fit the metric-plane mapping")
        raw_matrix = _normalise_homography(raw_matrix)
        width, height = image_size_px
        if width <= 1 or height <= 1:
            raise CalibrationError("image dimensions must be greater than one pixel")
        centre_px = ((width - 1) * 0.5, (height - 1) * 0.5)
        centre_grid = _project(raw_matrix, centre_px)
        translate = np.asarray(
            ((1.0, 0.0, -centre_grid[0]), (0.0, 1.0, -centre_grid[1]), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        matrix = _normalise_homography(translate @ raw_matrix)
        try:
            inverse = _normalise_homography(np.linalg.inv(matrix))
        except np.linalg.LinAlgError as exc:
            raise CalibrationError("metric-plane mapping is singular") from exc
        projected = cv2.perspectiveTransform(points.reshape(-1, 1, 2), matrix).reshape(
            -1, 2
        )
        centred_grid = grid - np.asarray(centre_grid)[None, :]
        errors = np.linalg.norm(projected - centred_grid, axis=1)
        top_y = _project(matrix, ((width - 1) * 0.5, 0.0))[1]
        bottom_y = _project(matrix, ((width - 1) * 0.5, height - 1.0))[1]
        return cls(
            source_path=path,
            source_sha256=hashlib.sha256(encoded).hexdigest(),
            image_size_px=(width, height),
            hole_pitch_mm=float(hole_pitch_mm),
            pixel_to_mm_matrix=matrix,
            mm_to_pixel_matrix=inverse,
            rms_error_mm=float(np.sqrt(np.mean(np.square(errors)))),
            max_error_mm=float(np.max(errors)),
            top_y_mm=float(top_y),
            bottom_y_mm=float(bottom_y),
        )


def _project(matrix: np.ndarray, point: tuple[float, float]) -> tuple[float, float]:
    homogeneous = matrix @ np.asarray((point[0], point[1], 1.0), dtype=np.float64)
    if not np.all(np.isfinite(homogeneous)) or abs(float(homogeneous[2])) < 1e-12:
        raise CalibrationError("point maps to projective infinity")
    return float(homogeneous[0] / homogeneous[2]), float(
        homogeneous[1] / homogeneous[2]
    )


def find_pinkplane_homography(video_path: Path) -> Path | None:
    """Locate the FastCap-copied homography beside a selected derivative."""

    resolved = video_path.resolve()
    directories = (
        (resolved / "postprocess", resolved / "calibration/geometry")
        if resolved.is_dir()
        else (resolved.parent,)
    )
    for directory in directories:
        candidate = directory / "homography.json"
        if candidate.is_file():
            return candidate
    directory = directories[0]
    report = directory / "report.json"
    if report.is_file():
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            value = payload["homography"]
            if isinstance(value, dict):
                value = value.get("path")
            if isinstance(value, str):
                resolved = Path(value).expanduser()
                if not resolved.is_absolute():
                    resolved = directory / resolved
                if resolved.is_file():
                    return resolved.resolve()
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None
