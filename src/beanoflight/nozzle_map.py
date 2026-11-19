"""Validated, immutable measured nozzle layout shared by replay and live runs."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .calibration import MetricPlaneCalibration
from .models import Gate
from .prediction import GateLayout


class NozzleMapError(ValueError):
    pass


def load_nozzle_layout(path: Path, calibration: MetricPlaneCalibration) -> GateLayout:
    """Fail closed on a selected but incompatible map; never fall back implicitly."""
    try:
        path = path.expanduser().resolve()
        encoded = path.read_bytes()
        data = json.loads(encoded)
        if (
            data["schema"] != "pinkplane-nozzle-map/v2"
            or data["measurement_camera"] != "CamL"
        ):
            raise ValueError("A CamL pinkplane-nozzle-map/v2 export is required")
        source = data["source"]
        if source["homography_sha256"] != calibration.source_sha256:
            raise ValueError("Nozzle map belongs to different camera geometry")
        geometry_bytes = calibration.source_path.read_bytes()
        if hashlib.sha256(geometry_bytes).hexdigest() != calibration.source_sha256:
            raise ValueError(
                "Active homography file changed after calibration was loaded"
            )
        geometry = json.loads(geometry_bytes)
        expected = geometry["metadata"]["intrinsic_bindings"]["CamL"]
        camera = source["cameras"]["CamL"]
        binding = camera["intrinsics"]
        if (
            not expected.get("intrinsic_sha256")
            or binding["intrinsic_sha256"] != expected["intrinsic_sha256"]
            or binding["logical_name"] != "CamL"
            or tuple(binding["image_size_px"]) != calibration.image_size_px
            or tuple(camera["image_size_px"]) != calibration.image_size_px
        ):
            raise ValueError(
                "Nozzle map CamL intrinsics or image dimensions do not match"
            )
        if not math.isclose(
            float(data["settings"]["hole_pitch_mm"]),
            calibration.hole_pitch_mm,
            abs_tol=1e-9,
            rel_tol=0,
        ):
            raise ValueError("Nozzle map grid pitch differs from active calibration")
        if (
            data["coordinates"]["units"] != "mm"
            or data["coordinates"]["tracking"]["camera"] != "CamL"
        ):
            raise ValueError("Nozzle map coordinate domain is not CamL millimetres")
        matrix = np.asarray(
            data["coordinates"]["tracking"]["pixel_to_mm_matrix"], dtype=float
        )
        if matrix.shape != (3, 3) or not np.allclose(
            matrix, calibration.pixel_to_mm_matrix, rtol=1e-7, atol=1e-7
        ):
            raise ValueError(
                "Nozzle map tracking transform differs from active calibration"
            )
        nozzles = data["nozzles"]
        if not isinstance(nozzles, list) or len(nozzles) not in range(3, 22, 2):
            raise ValueError("Expected an odd nozzle count from 3 to 21")
        if data["settings"]["expected_count"] != len(nozzles):
            raise ValueError("Nozzle list does not match the captured count")
        centres, ids, channels = [], [], []
        for nozzle in nozzles:
            nozzle_id, channel = nozzle["nozzle_id"], nozzle["valve_channel"]
            if type(nozzle_id) is not int or nozzle_id <= 0 or nozzle_id in ids:
                raise ValueError("Nozzle IDs must be unique positive integers")
            if channel is not None and (
                type(channel) is not int
                or not 0 <= channel <= 20
                or channel in channels
            ):
                raise ValueError(
                    "Valve channels must be null or unique output bit numbers 0–20 (not GPIO numbers)"
                )
            if nozzle["measurement_camera"] != "CamL":
                raise ValueError("Every measured nozzle must reference CamL")
            pixels = np.asarray(
                nozzle["cameras"]["CamL"]["intersection_undistorted_px"], dtype=float
            )
            saved = np.asarray(nozzle["intersection_tracking_mm"], dtype=float)
            if (
                pixels.shape != (2,)
                or saved.shape != (2,)
                or not np.all(np.isfinite([pixels, saved]))
            ):
                raise ValueError("Invalid nozzle intersection coordinates")
            point = calibration.pixel_to_mm(tuple(pixels))
            if not np.allclose(saved, point, rtol=0, atol=1e-4):
                raise ValueError(
                    "Saved nozzle coordinates disagree with the active tracking transform"
                )
            centres.append(point)
            ids.append(nozzle_id)
            channels.append(channel)
        xs = np.asarray([point[0] for point in centres])
        if np.any(np.diff(xs) <= 0):
            raise ValueError(
                "Nozzles must be ordered by strictly increasing physical x"
            )
        boundaries = np.concatenate(
            (
                [xs[0] - (xs[1] - xs[0]) / 2],
                (xs[:-1] + xs[1:]) / 2,
                [xs[-1] + (xs[-1] - xs[-2]) / 2],
            )
        )
        gates = tuple(
            Gate(
                nozzle_id,
                float(boundaries[i]),
                float(boundaries[i + 1]),
                centres[i][0],
                centres[i][1],
                nozzle_id,
                channels[i],
            )
            for i, nozzle_id in enumerate(ids)
        )
        return GateLayout(
            min(p[1] for p in centres),
            gate_count=len(gates),
            measured_gates=gates,
            nozzle_map_sha256=hashlib.sha256(encoded).hexdigest(),
            nozzle_map_path=str(path),
            nozzle_map_json=json.dumps(data, allow_nan=False),
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise NozzleMapError(f"Cannot use nozzle map {path}: {exc}") from exc


def resolve_gate_layout(calibration, nozzle_map_path=None, sorting_offset_mm=30.0):
    return (
        load_nozzle_layout(Path(nozzle_map_path), calibration)
        if nozzle_map_path is not None
        else GateLayout(calibration.sorting_line_y(sorting_offset_mm))
    )
