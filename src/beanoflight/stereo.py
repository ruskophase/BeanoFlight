"""Stereo point mapping and crop-pair metadata.

The PinkPlane homography lives in undistorted pixel coordinates.  Optimized
RAW replay deliberately keeps both images in their distorted sensor domain,
so only the selected centroid is undistorted, transferred through the
homography, and distorted into CamR coordinates.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


class StereoCalibrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StereoPairMetadata:
    left_frame_index: int
    right_frame_index: int
    left_timestamp_ns: int
    right_timestamp_ns: int
    caml_centroid_px: tuple[float, float]
    camr_projected_centroid_px: tuple[float, float]
    camr_centroid_px: tuple[float, float]
    refinement_distance_px: float
    refinement_area_px: int
    coordinate_domain: str = "distorted-raw"

    @property
    def synchronization_delta_ns(self) -> int:
        return self.right_timestamp_ns - self.left_timestamp_ns

    def to_json(self) -> dict[str, object]:
        return {
            "left_frame_index": self.left_frame_index,
            "right_frame_index": self.right_frame_index,
            "left_timestamp_ns": self.left_timestamp_ns,
            "right_timestamp_ns": self.right_timestamp_ns,
            "synchronization_delta_ns": self.synchronization_delta_ns,
            "caml_centroid_px": list(self.caml_centroid_px),
            "camr_projected_centroid_px": list(
                self.camr_projected_centroid_px
            ),
            "camr_centroid_px": list(self.camr_centroid_px),
            "refinement_distance_px": self.refinement_distance_px,
            "refinement_area_px": self.refinement_area_px,
            "coordinate_domain": self.coordinate_domain,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> StereoPairMetadata:
        def point(name: str) -> tuple[float, float]:
            raw = value.get(name)
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise StereoCalibrationError(f"{name} must contain two values")
            result = float(raw[0]), float(raw[1])
            if not all(math.isfinite(item) for item in result):
                raise StereoCalibrationError(f"{name} must be finite")
            return result

        result = cls(
            left_frame_index=int(value["left_frame_index"]),
            right_frame_index=int(value["right_frame_index"]),
            left_timestamp_ns=int(value["left_timestamp_ns"]),
            right_timestamp_ns=int(value["right_timestamp_ns"]),
            caml_centroid_px=point("caml_centroid_px"),
            camr_projected_centroid_px=point("camr_projected_centroid_px"),
            camr_centroid_px=point("camr_centroid_px"),
            refinement_distance_px=float(value["refinement_distance_px"]),
            refinement_area_px=int(value["refinement_area_px"]),
            coordinate_domain=str(value.get("coordinate_domain", "distorted-raw")),
        )
        if (
            result.left_frame_index < 0
            or result.right_frame_index < 0
            or result.refinement_area_px <= 0
            or not math.isfinite(result.refinement_distance_px)
            or result.refinement_distance_px < 0
        ):
            raise StereoCalibrationError("stereo pair metadata is invalid")
        if (
            "synchronization_delta_ns" in value
            and int(value["synchronization_delta_ns"])
            != result.synchronization_delta_ns
        ):
            raise StereoCalibrationError("stereo synchronization delta is invalid")
        return result


@dataclass(frozen=True, slots=True)
class StereoCropPreparation:
    caml_materializer: Callable[[], np.ndarray]
    camr_materializer: Callable[[], np.ndarray]
    width_px: int
    height_px: int
    source_size_px: int
    padded: bool
    pair: StereoPairMetadata
    camr_mask: np.ndarray | None = None


@dataclass(frozen=True, slots=True)
class StereoPointCalibration:
    homography_path: Path
    caml_to_camr_undistorted: np.ndarray
    camr_to_caml_undistorted: np.ndarray
    caml_camera_matrix: np.ndarray
    caml_distortion: np.ndarray
    camr_camera_matrix: np.ndarray
    camr_distortion: np.ndarray

    @classmethod
    def load(
        cls,
        homography_path: Path,
        caml_profile: Mapping[str, object],
        camr_profile: Mapping[str, object],
    ) -> StereoPointCalibration:
        path = homography_path.expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            mapping = payload["mapping"]
            direction = str(mapping["direction"])
            domain = str(mapping["coordinate_domain"])
            homography = _matrix(mapping["matrix"], (3, 3), "homography")
            caml = caml_profile["calibration"]
            camr = camr_profile["calibration"]
            caml_matrix = _matrix(caml["camera_matrix"], (3, 3), "CamL matrix")
            camr_matrix = _matrix(camr["camera_matrix"], (3, 3), "CamR matrix")
            caml_distortion = _distortion(caml["distortion_coefficients"], "CamL")
            camr_distortion = _distortion(camr["distortion_coefficients"], "CamR")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StereoCalibrationError(
                f"cannot load stereo point calibration: {exc}"
            ) from exc
        if payload.get("schema") != "pinkplane-homography/v2":
            raise StereoCalibrationError("stereo mapping must be PinkPlane v2")
        if domain != "undistorted":
            raise StereoCalibrationError("stereo mapping must be undistorted")
        if "CamL" not in direction or "CamR" not in direction:
            raise StereoCalibrationError("stereo mapping direction must be CamL to CamR")
        if abs(float(np.linalg.det(homography))) < 1e-12:
            raise StereoCalibrationError("stereo homography is singular")
        return cls(
            path,
            homography / homography[2, 2],
            np.linalg.inv(homography),
            caml_matrix,
            caml_distortion,
            camr_matrix,
            camr_distortion,
        )

    def project_distorted_caml_to_distorted_camr(
        self, point: tuple[float, float]
    ) -> tuple[float, float]:
        caml_undistorted = cv2.undistortPoints(
            np.asarray(point, dtype=np.float64).reshape(1, 1, 2),
            self.caml_camera_matrix,
            self.caml_distortion,
            P=self.caml_camera_matrix,
        )
        camr_undistorted = cv2.perspectiveTransform(
            caml_undistorted, self.caml_to_camr_undistorted
        ).reshape(2)
        homogeneous = np.linalg.solve(
            self.camr_camera_matrix,
            np.asarray(
                (camr_undistorted[0], camr_undistorted[1], 1.0),
                dtype=np.float64,
            ),
        )
        if abs(float(homogeneous[2])) < 1e-12:
            raise StereoCalibrationError("CamR point maps to projective infinity")
        normalized = homogeneous[:2] / homogeneous[2]
        projected, _jacobian = cv2.projectPoints(
            np.asarray(((normalized[0], normalized[1], 1.0),), dtype=np.float64),
            np.zeros(3, dtype=np.float64),
            np.zeros(3, dtype=np.float64),
            self.camr_camera_matrix,
            self.camr_distortion,
        )
        x, y = projected.reshape(2)
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise StereoCalibrationError("stereo projection is not finite")
        return float(x), float(y)

    def project_distorted_camr_to_undistorted_caml(
        self, point: tuple[float, float]
    ) -> tuple[float, float]:
        """Map a distorted CamR point back into CamL's metric-plane domain."""

        camr_undistorted = cv2.undistortPoints(
            np.asarray(point, dtype=np.float64).reshape(1, 1, 2),
            self.camr_camera_matrix,
            self.camr_distortion,
            P=self.camr_camera_matrix,
        )
        caml_undistorted = cv2.perspectiveTransform(
            camr_undistorted, self.camr_to_caml_undistorted
        ).reshape(2)
        x, y = (float(value) for value in caml_undistorted)
        if not math.isfinite(x) or not math.isfinite(y):
            raise StereoCalibrationError("reverse stereo projection is not finite")
        return x, y


def _matrix(value: object, shape: tuple[int, int], name: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != shape or not np.all(np.isfinite(matrix)):
        raise StereoCalibrationError(f"{name} must be finite with shape {shape}")
    return matrix


def _distortion(value: object, camera: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64).reshape(-1)
    if result.size < 4 or not np.all(np.isfinite(result)):
        raise StereoCalibrationError(f"{camera} distortion is invalid")
    return result
