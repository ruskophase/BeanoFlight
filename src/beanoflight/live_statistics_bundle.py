"""Offline Statistics Bundles from inference-attached live evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .live_statistics import (
    BEAN_LEDGER_SCHEMA,
    CAPTURE_SCHEMA,
    OBSERVATION_SCHEMA,
)
from .source import SourceError
from .statistics_bundle import (
    _chart_canvas,
    _file_inventory,
    _git_commit,
    _histogram,
    _panel,
    _read_json,
    _scatter,
    _sha256,
    _write_csv,
    _write_json,
    _write_jsonl,
)
from .statistics_dashboard import write_statistics_dashboard
from .statistics_features import numeric_summary

BUNDLE_SCHEMA = "beanoflight-live-statistics-bundle/v1"
DERIVED_OBSERVATION_SCHEMA = "beanoflight-live-statistics-derived-observation/v1"
DERIVED_BEAN_SCHEMA = "beanoflight-live-statistics-derived-bean/v1"


@dataclass(frozen=True, slots=True)
class _MeanColourCalibration:
    white_level: float
    dark_level: float
    white_balance_rgb: np.ndarray
    colour_matrix_rgb: np.ndarray

    @classmethod
    def load(cls, profile_path: Path) -> _MeanColourCalibration:
        profile = _read_json(profile_path)
        capture = profile.get("capture", {})
        calibration = profile.get("calibration", {})
        white_level = float(capture["decoded_white_level"])
        dark_level = float(calibration.get("dark_level_median", 0.0))
        if not math.isfinite(white_level) or white_level <= dark_level:
            raise SourceError(f"invalid radiometric range in {profile_path}")
        white_balance = (
            np.asarray(calibration["wb_gains_rgb"], dtype=np.float64)
            if calibration.get("wb_enabled", False)
            else np.ones(3, dtype=np.float64)
        )
        colour_matrix = (
            np.asarray(calibration["color_matrix_rgb"], dtype=np.float64)
            if calibration.get("color_matrix_enabled", False)
            else np.eye(3, dtype=np.float64)
        )
        if (
            white_balance.shape != (3,)
            or colour_matrix.shape != (3, 3)
            or not np.all(np.isfinite(white_balance))
            or not np.all(np.isfinite(colour_matrix))
        ):
            raise SourceError(f"invalid colour calibration in {profile_path}")
        return cls(
            white_level,
            dark_level,
            white_balance,
            colour_matrix,
        )

    def transform_mean_bgr(self, bgr: Sequence[float]) -> dict[str, float]:
        """Approximately calibrate an aggregate sensor-space BGR mean.

        Live evidence deliberately retains aggregates rather than pixels. The
        transform can apply global dark, white-balance and matrix terms, but
        cannot reconstruct spatial flat-field or defect correction or commute
        nonlinear colour conversion through a pixel distribution.
        """

        encoded = np.asarray(tuple(bgr), dtype=np.float64)
        if encoded.shape != (3,) or not np.all(np.isfinite(encoded)):
            raise ValueError("mean BGR must contain three finite channels")
        sensor_rgb = encoded[::-1] / 255.0 * self.white_level
        linear_rgb = np.clip(
            (sensor_rgb - self.dark_level)
            / max(self.white_level - self.dark_level, 1.0),
            0.0,
            1.0,
        )
        linear_rgb *= self.white_balance_rgb
        linear_rgb = np.clip(self.colour_matrix_rgb @ linear_rgb, 0.0, 1.0)
        srgb = np.where(
            linear_rgb <= 0.0031308,
            linear_rgb * 12.92,
            1.055 * np.power(linear_rgb, 1.0 / 2.4) - 0.055,
        )
        lab = cv2.cvtColor(srgb.astype(np.float32).reshape(1, 1, 3), cv2.COLOR_RGB2LAB)[
            0, 0
        ].astype(np.float64)
        display_bgr = np.clip(srgb[::-1] * 255.0 + 0.5, 0, 255).astype(np.uint8)
        denominator = max(float(np.sum(linear_rgb)), 1e-12)
        chromaticity = linear_rgb / denominator
        return {
            "approx_calibrated_mean_b": float(display_bgr[0]),
            "approx_calibrated_mean_g": float(display_bgr[1]),
            "approx_calibrated_mean_r": float(display_bgr[2]),
            "approx_lab_l": float(lab[0]),
            "approx_lab_a": float(lab[1]),
            "approx_lab_b": float(lab[2]),
            "approx_lab_chroma": float(math.hypot(lab[1], lab[2])),
            "approx_linear_red_chromaticity": float(chromaticity[0]),
            "approx_linear_green_chromaticity": float(chromaticity[1]),
            "approx_linear_blue_chromaticity": float(chromaticity[2]),
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beano-live-statistics-bundle",
        description=(
            "Create offline colour/size Statistics Bundles from completed "
            "inference-attached numerical live captures."
        ),
    )
    result.add_argument("captures", type=Path, nargs="+")
    destination = result.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path)
    destination.add_argument("--output-root", type=Path)
    result.add_argument("--calibration-pack", type=Path)
    result.add_argument(
        "--run-report",
        type=Path,
        help="optional performance report used for compact processing-health evidence",
    )
    result.add_argument("--overwrite", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> None:
    command_parser = parser()
    arguments = command_parser.parse_args(argv)
    if arguments.output is not None and len(arguments.captures) != 1:
        command_parser.error("--output requires exactly one capture")
    outputs = []
    for capture in arguments.captures:
        resolved = capture.expanduser().resolve()
        output = (
            arguments.output.expanduser().resolve()
            if arguments.output is not None
            else (
                arguments.output_root.expanduser().resolve()
                / f"{resolved.name}-statistics-bundle"
                if arguments.output_root is not None
                else resolved / "offline-statistics-bundle"
            )
        )
        outputs.append(
            build_live_statistics_bundle(
                resolved,
                output,
                calibration_pack=arguments.calibration_pack,
                run_report=arguments.run_report,
                overwrite=arguments.overwrite,
            )
        )
    print(json.dumps(outputs, indent=2), flush=True)


def build_live_statistics_bundle(
    capture_directory: Path,
    output: Path,
    *,
    calibration_pack: Path | None = None,
    run_report: Path | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    capture_directory = capture_directory.expanduser().resolve()
    output = output.expanduser().resolve()
    capture_path = capture_directory / "capture.json"
    observations_path = capture_directory / "observations.jsonl"
    ledger_path = capture_directory / "beans.jsonl"
    capture = _read_json(capture_path)
    _validate_capture(capture_directory, capture)
    observations = _read_jsonl(observations_path, OBSERVATION_SCHEMA)
    ledger = _read_jsonl(ledger_path, BEAN_LEDGER_SCHEMA)
    run_id = str(capture.get("run_id", ""))
    if not run_id:
        raise SourceError("live statistics capture has no run ID")
    if any(str(row.get("run_id", "")) != run_id for row in observations):
        raise SourceError("observation run ID does not match capture")
    if any(str(row.get("run_id", "")) != run_id for row in ledger):
        raise SourceError("bean-ledger run ID does not match capture")
    statistics = capture.get("statistics", {})
    if len(observations) != int(statistics.get("total_observations_persisted", -1)):
        raise SourceError("observation row count does not match capture")
    if len(ledger) != int(statistics.get("confirmed_beans", -1)):
        raise SourceError("bean-ledger row count does not match capture")

    calibration_root = _calibration_root(capture, calibration_pack)
    calibrations = {
        camera: _MeanColourCalibration.load(calibration_root / camera / "profile.json")
        for camera in ("CamL", "CamR")
    }
    confirmed_bean_ids = {str(row["bean_id"]) for row in ledger}
    derived_observations = [
        _derive_observation(row, calibrations)
        for row in observations
        if str(row["bean_id"]) in confirmed_bean_ids
    ]
    by_bean: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in derived_observations:
        by_bean[str(row["bean_id"])].append(row)
    beans = _aggregate_live_beans(ledger, by_bean)
    dark_summary = _score_live_appearance(beans)
    dark_candidates = sorted(
        (bean for bean in beans if bean["dark_candidate_2sd"]),
        key=lambda bean: float(bean["combined_approx_lab_l_mean"]),
    )

    if output.exists() and not overwrite:
        raise FileExistsError(f"Statistics Bundle already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.tmp-{os.getpid()}"
    if temporary.exists():
        raise FileExistsError(f"temporary bundle path already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        charts = temporary / "charts"
        charts.mkdir()
        _write_csv(temporary / "observations.csv", derived_observations)
        _write_jsonl(temporary / "observations.jsonl", derived_observations)
        _write_csv(temporary / "beans.csv", beans)
        _write_jsonl(temporary / "beans.jsonl", beans)
        _write_csv(temporary / "dark-bean-candidates.csv", dark_candidates)
        _write_jsonl(temporary / "dark-bean-candidates.jsonl", dark_candidates)
        _write_live_charts(charts, beans, dark_summary)
        summary = _live_summary(
            capture,
            beans,
            derived_observations,
            dark_summary,
            source_observation_count=len(observations),
            runtime=_compact_runtime_report(run_report, run_id),
        )
        dashboard = write_statistics_dashboard(
            temporary / "dashboard",
            beans=beans,
            summary=summary,
            source_fps=_source_fps(calibration_root),
        )
        _write_json(temporary / "summary.json", summary)
        (temporary / "README.md").write_text(
            _bundle_readme(capture_directory.name), encoding="utf-8"
        )
        manifest = {
            "schema": BUNDLE_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_capture": str(capture_directory),
            "source_run_id": run_id,
            "schemas": {
                "source_capture": CAPTURE_SCHEMA,
                "source_observations": OBSERVATION_SCHEMA,
                "source_bean_ledger": BEAN_LEDGER_SCHEMA,
                "derived_observations": DERIVED_OBSERVATION_SCHEMA,
                "derived_beans": DERIVED_BEAN_SCHEMA,
            },
            "provenance": {
                "capture": {
                    "path": str(capture_path),
                    "sha256": _sha256(capture_path),
                    "classification": capture.get("provenance", {}).get(
                        "classification"
                    ),
                    "live_test_override": capture.get("provenance", {}).get(
                        "live_test_override"
                    ),
                },
                "source_files": capture.get("files", {}),
                "calibration_pack": str(calibration_root),
                "calibration_profiles": {
                    camera: {
                        "path": str(calibration_root / camera / "profile.json"),
                        "sha256": _sha256(calibration_root / camera / "profile.json"),
                    }
                    for camera in ("CamL", "CamR")
                },
                "run_report": (
                    {
                        "path": str(run_report.expanduser().resolve()),
                        "sha256": _sha256(run_report.expanduser().resolve()),
                    }
                    if run_report is not None
                    else None
                ),
                "colour_reconstruction": (
                    "Approximate transformation of each masked linear "
                    "sensor-BGR channel mean through global dark subtraction, "
                    "white balance, colour matrix, sRGB encoding and "
                    "floating-point CIE Lab conversion. Spatial "
                    "flat-field/defect correction and per-pixel nonlinear "
                    "colour statistics cannot be reconstructed from retained "
                    "aggregates."
                ),
                "software": {
                    "package": "beanoflight",
                    "git_commit": _git_commit(),
                    "opencv": cv2.__version__,
                    "numpy": np.__version__,
                },
            },
            "definitions": _definitions(),
            "summary": summary,
            "dashboard": dashboard,
            "files": _file_inventory(temporary),
        }
        _write_json(temporary / "manifest.json", manifest)
        if output.exists():
            shutil.rmtree(output)
        temporary.replace(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "source_capture": str(capture_directory),
        "output": str(output),
        "confirmed_beans": len(beans),
        "observations": len(derived_observations),
        "dark_candidates_2sd": len(dark_candidates),
        "dark_lightness_threshold": dark_summary["threshold_mean_minus_2sd"],
        "dashboard": str(output / "dashboard" / "index.html"),
    }


def _validate_capture(root: Path, capture: Mapping[str, Any]) -> None:
    if capture.get("schema") != CAPTURE_SCHEMA:
        raise SourceError("unsupported live statistics capture schema")
    if capture.get("status") != "completed":
        raise SourceError("live statistics capture is not complete")
    files = capture.get("files", {})
    for name in ("observations.jsonl", "beans.jsonl"):
        descriptor = files.get(name, {})
        path = root / name
        if not path.is_file():
            raise SourceError(f"live statistics capture is missing {name}")
        expected_size = int(descriptor.get("bytes", -1))
        expected_hash = str(descriptor.get("sha256", ""))
        if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
            raise SourceError(f"live statistics capture integrity failed: {name}")


def _read_jsonl(path: Path, schema: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("schema") != schema:
                    raise SourceError(f"unsupported row at {path}:{line_number}")
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc
    return rows


def _calibration_root(capture: Mapping[str, Any], explicit: Path | None) -> Path:
    value = explicit
    if value is None:
        provenance = capture.get("provenance", {})
        source = provenance.get("calibration_or_recording")
        if not isinstance(source, str) or not source:
            raise SourceError("capture does not identify its calibration pack")
        value = Path(source)
    root = value.expanduser().resolve()
    for camera in ("CamL", "CamR"):
        if not (root / camera / "profile.json").is_file():
            raise SourceError(f"calibration pack is missing {camera}/profile.json")
    return root


def _derive_observation(
    source: Mapping[str, Any],
    calibrations: Mapping[str, _MeanColourCalibration],
) -> dict[str, Any]:
    row = dict(source)
    row["source_schema"] = row.get("schema")
    row["schema"] = DERIVED_OBSERVATION_SCHEMA
    scale = _finite_value(row.get("mask_scale_to_native"), default=1.0)
    for prefix, camera in (("caml", "CamL"), ("camr", "CamR")):
        available = bool(row.get(f"{prefix}_measurement_available", False))
        if not available:
            continue
        _derive_view_geometry(row, prefix, scale)
        means = tuple(
            _finite_value(row.get(f"{prefix}_{channel}_mean"))
            for channel in ("b", "g", "r")
        )
        if not all(math.isfinite(value) for value in means):
            continue
        transformed = calibrations[camera].transform_mean_bgr(means)
        row.update({f"{prefix}_{key}": value for key, value in transformed.items()})

    for key in (
        "approx_lab_l",
        "approx_lab_a",
        "approx_lab_b",
        "approx_lab_chroma",
    ):
        row[f"combined_{key}"] = _mean_finite(
            row.get(f"caml_{key}"), row.get(f"camr_{key}")
        )
    row["approx_lab_l_view_delta"] = _difference(
        row.get("camr_approx_lab_l"), row.get("caml_approx_lab_l")
    )
    area_left = _finite_value(row.get("caml_area_native_px"))
    area_right = _finite_value(row.get("camr_area_native_px"))
    area_values = _positive_values(area_left, area_right)
    if area_values:
        area = (
            math.sqrt(area_values[0] * area_values[1])
            if len(area_values) == 2
            else area_values[0]
        )
        row["projected_area_proxy_px"] = area
        row["projected_area_proxy_view_count"] = len(area_values)
        row["equivalent_sphere_volume_proxy_px3"] = (
            4.0 * area**1.5 / (3.0 * math.sqrt(math.pi))
        )
    if len(area_values) == 2:
        row["projected_area_geomean_px"] = area
        row["projected_area_ratio_camr_to_caml"] = area_right / max(area_left, 1e-12)

    major_values = _positive_values(
        row.get("caml_ellipse_major_native_px"),
        row.get("camr_ellipse_major_native_px"),
    )
    minor_values = _positive_values(
        row.get("caml_ellipse_minor_native_px"),
        row.get("camr_ellipse_minor_native_px"),
    )
    if major_values and minor_values:
        major = _one_or_geometric_mean(major_values)
        minor = _one_or_geometric_mean(minor_values)
        row["rotational_ellipsoid_volume_proxy_px3"] = math.pi * major * minor**2 / 6.0
    return row


def _derive_view_geometry(row: dict[str, Any], prefix: str, scale: float) -> None:
    mask_area = _finite_value(row.get(f"{prefix}_mask_area_px"))
    native_mask_area = mask_area * scale**2
    source_area_key = (
        "caml_detection_area_px" if prefix == "caml" else "camr_refinement_area_px"
    )
    source_area = _finite_value(row.get(source_area_key))
    row[f"{prefix}_mask_area_native_px"] = native_mask_area
    row[f"{prefix}_area_native_px"] = (
        source_area
        if math.isfinite(source_area) and source_area > 0
        else native_mask_area
    )
    covariance = np.asarray(
        (
            (
                _finite_value(row.get(f"{prefix}_mask_variance_x_px2"), 0.0),
                _finite_value(row.get(f"{prefix}_mask_covariance_xy_px2"), 0.0),
            ),
            (
                _finite_value(row.get(f"{prefix}_mask_covariance_xy_px2"), 0.0),
                _finite_value(row.get(f"{prefix}_mask_variance_y_px2"), 0.0),
            ),
        ),
        dtype=np.float64,
    )
    eigenvalues = np.maximum(np.linalg.eigvalsh(covariance), 0.0)
    row[f"{prefix}_ellipse_minor_native_px"] = float(
        4.0 * math.sqrt(eigenvalues[0]) * scale
    )
    row[f"{prefix}_ellipse_major_native_px"] = float(
        4.0 * math.sqrt(eigenvalues[1]) * scale
    )


def _aggregate_live_beans(
    ledger: Sequence[Mapping[str, Any]],
    groups: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    beans: list[dict[str, Any]] = []
    for source in sorted(ledger, key=lambda row: int(row["bean_sequence"])):
        bean_id = str(source["bean_id"])
        rows = groups.get(bean_id, ())
        bean: dict[str, Any] = dict(source)
        bean["source_schema"] = bean.get("schema")
        bean["schema"] = DERIVED_BEAN_SCHEMA
        bean["derived_observation_count"] = len(rows)
        view_counts = [
            int(value)
            for row in rows
            if math.isfinite(value := _finite_value(row.get("measurement_view_count")))
        ]
        bean["minimum_measurement_view_count"] = min(view_counts) if view_counts else 0
        bean["sensor_edge_observation_count"] = sum(
            bool(row.get("caml_detection_touches_sensor_edge", False)) for row in rows
        )
        bean["enrichment_fallback_observation_count"] = sum(
            not bool(row.get("feature_enrichment_valid", True)) for row in rows
        )
        for camera in ("caml", "camr"):
            for key in (
                "area_native_px",
                "mask_area_native_px",
                "ellipse_minor_native_px",
                "ellipse_major_native_px",
                "approx_calibrated_mean_b",
                "approx_calibrated_mean_g",
                "approx_calibrated_mean_r",
                "approx_lab_l",
                "approx_lab_a",
                "approx_lab_b",
                "approx_lab_chroma",
            ):
                bean[f"{camera}_{key}_median"] = _median(rows, f"{camera}_{key}")
        for key in (
            "combined_approx_lab_l",
            "combined_approx_lab_a",
            "combined_approx_lab_b",
            "combined_approx_lab_chroma",
            "approx_lab_l_view_delta",
            "projected_area_proxy_px",
            "projected_area_proxy_view_count",
            "projected_area_geomean_px",
            "projected_area_ratio_camr_to_caml",
            "equivalent_sphere_volume_proxy_px3",
            "rotational_ellipsoid_volume_proxy_px3",
        ):
            suffix = "mean" if key.startswith("combined_") else "median"
            bean[f"{key}_{suffix}"] = _median(rows, key)
        beans.append(bean)
    return beans


def _score_live_appearance(beans: list[dict[str, Any]]) -> dict[str, Any]:
    lightness = np.asarray(
        [float(bean["combined_approx_lab_l_mean"]) for bean in beans],
        dtype=np.float64,
    )
    finite_lightness = lightness[np.isfinite(lightness)]
    mean = float(np.mean(finite_lightness)) if finite_lightness.size else math.nan
    standard_deviation = (
        float(np.std(finite_lightness, ddof=1))
        if finite_lightness.size >= 2
        else math.nan
    )
    threshold = (
        mean - 2.0 * standard_deviation
        if math.isfinite(standard_deviation)
        else math.nan
    )
    robust_centre = (
        float(np.median(finite_lightness)) if finite_lightness.size else math.nan
    )
    robust_sigma = (
        float(np.median(np.abs(finite_lightness - robust_centre)) * 1.4826)
        if finite_lightness.size
        else math.nan
    )
    robust_threshold = (
        robust_centre - 2.0 * robust_sigma if math.isfinite(robust_sigma) else math.nan
    )

    feature_keys = (
        "combined_approx_lab_l_mean",
        "combined_approx_lab_a_mean",
        "combined_approx_lab_b_mean",
    )
    values = np.asarray(
        [[float(bean[key]) for key in feature_keys] for bean in beans],
        dtype=np.float64,
    )
    complete = np.all(np.isfinite(values), axis=1) if len(values) else np.asarray(())
    complete_values = values[complete] if len(values) else np.empty((0, 3))
    medians = (
        np.median(complete_values, axis=0) if len(complete_values) else np.zeros(3)
    )
    mad = (
        np.median(np.abs(complete_values - medians), axis=0) * 1.4826
        if len(complete_values)
        else np.ones(3)
    )
    scale = np.maximum(mad, np.asarray((3.0, 2.0, 2.0)))
    scores = np.full(len(values), np.nan, dtype=np.float64)
    if len(complete_values):
        scores[complete] = np.sqrt(
            np.mean(np.square((complete_values - medians) / scale), axis=1)
        )
    finite_score_indices = np.flatnonzero(np.isfinite(scores))
    percentiles = np.full(len(scores), np.nan, dtype=np.float64)
    if finite_score_indices.size:
        ordered = finite_score_indices[np.argsort(scores[finite_score_indices])]
        percentiles[ordered] = (np.arange(len(ordered)) + 1) / len(ordered) * 100.0
    for index, bean in enumerate(beans):
        value = float(bean["combined_approx_lab_l_mean"])
        z_score = (
            (value - mean) / standard_deviation
            if math.isfinite(value)
            and math.isfinite(standard_deviation)
            and standard_deviation > 0
            else math.nan
        )
        robust_z = (
            (value - robust_centre) / robust_sigma
            if math.isfinite(value) and math.isfinite(robust_sigma) and robust_sigma > 0
            else math.nan
        )
        candidate = bool(math.isfinite(threshold) and value <= threshold)
        robust_candidate = bool(
            math.isfinite(robust_threshold) and value <= robust_threshold
        )
        flags = []
        if candidate:
            flags.append("dark-candidate-mean-minus-2sd")
        if robust_candidate:
            flags.append("dark-candidate-robust-median-minus-2mad-sigma")
        if math.isfinite(scores[index]) and (
            scores[index] >= 4.0 or percentiles[index] >= 99.0
        ):
            flags.append("appearance-outlier")
        bean["lightness_z_score"] = z_score
        bean["robust_lightness_z_score"] = robust_z
        bean["dark_candidate_2sd"] = candidate
        bean["dark_candidate_robust"] = robust_candidate
        bean["appearance_outlier_score"] = (
            float(scores[index]) if math.isfinite(scores[index]) else math.nan
        )
        bean["appearance_outlier_percentile"] = (
            float(percentiles[index]) if math.isfinite(percentiles[index]) else math.nan
        )
        bean["appearance_flags"] = ";".join(flags)
    return {
        "method": "one-sided combined approximate Lab L* <= batch mean - 2 sample standard deviations",
        "lightness_mean": mean,
        "lightness_sample_standard_deviation": standard_deviation,
        "threshold_mean_minus_2sd": threshold,
        "candidate_count": sum(bool(bean["dark_candidate_2sd"]) for bean in beans),
        "candidate_fraction": (
            sum(bool(bean["dark_candidate_2sd"]) for bean in beans) / max(len(beans), 1)
        ),
        "robust_reference": {
            "centre_median": robust_centre,
            "mad_equivalent_sigma": robust_sigma,
            "threshold_median_minus_2mad_sigma": robust_threshold,
            "candidate_count": sum(
                bool(bean["dark_candidate_robust"]) for bean in beans
            ),
        },
        "classification_status": (
            "provisional within-batch review candidates; requires labelled "
            "known-good and dark-bean calibration before sorting use"
        ),
    }


def _live_summary(
    capture: Mapping[str, Any],
    beans: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    dark_summary: Mapping[str, Any],
    *,
    source_observation_count: int,
    runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        result = []
        for bean in beans:
            value = _finite_value(bean.get(key))
            if math.isfinite(value):
                result.append(value)
        return result

    return {
        "source_run_id": capture.get("run_id"),
        "source_classification": capture.get("provenance", {}).get("classification"),
        "counts": {
            "confirmed_beans": len(beans),
            "derived_observations": len(observations),
            "unconfirmed_source_observations_excluded": (
                source_observation_count - len(observations)
            ),
            "beans_with_two_samples": sum(
                int(bean.get("sample_count", 0)) == 2 for bean in beans
            ),
            "beans_with_one_sample": sum(
                int(bean.get("sample_count", 0)) == 1 for bean in beans
            ),
            "beans_without_samples": sum(
                int(bean.get("sample_count", 0)) == 0 for bean in beans
            ),
            "beans_with_colour": sum(
                math.isfinite(_finite_value(bean.get("combined_approx_lab_l_mean")))
                for bean in beans
            ),
            "beans_with_sensor_edge_observation": sum(
                bool(bean.get("sensor_edge_observation_count", 0)) for bean in beans
            ),
            "beans_with_enrichment_fallback": sum(
                bool(bean.get("enrichment_fallback_observation_count", 0))
                for bean in beans
            ),
        },
        "distributions": {
            key: numeric_summary(values(key))
            for key in (
                "combined_approx_lab_l_mean",
                "combined_approx_lab_a_mean",
                "combined_approx_lab_b_mean",
                "projected_area_proxy_px_median",
                "equivalent_sphere_volume_proxy_px3_median",
                "rotational_ellipsoid_volume_proxy_px3_median",
                "projected_area_ratio_camr_to_caml_median",
                "approx_lab_l_view_delta_median",
                "appearance_outlier_score",
            )
        },
        "dark_bean_screen": dict(dark_summary),
        "source_capture_statistics": capture.get("statistics", {}),
        "runtime": dict(runtime) if runtime is not None else None,
    }


def _source_fps(calibration_root: Path) -> float:
    profile = _read_json(calibration_root / "CamL" / "profile.json")
    return _finite_value(
        profile.get("capture", {}).get("controls", {}).get("frame_rate_hz"),
        60.0,
    )


def _compact_runtime_report(
    report_path: Path | None, run_id: str
) -> dict[str, Any] | None:
    if report_path is None:
        return None
    resolved = report_path.expanduser().resolve()
    report = _read_json(resolved)
    runs = report.get("runs", ())
    if not isinstance(runs, list):
        raise SourceError(f"performance report has no runs: {resolved}")
    match = next(
        (
            run
            for run in runs
            if isinstance(run, dict)
            and str(run.get("summary", {}).get("run_id", "")) == run_id
        ),
        None,
    )
    if match is None:
        raise SourceError(f"performance report has no matching run ID {run_id}")
    source_summary = match.get("summary", {})
    outcome = match.get("outcome", {})
    summary_keys = (
        "frames_processed",
        "elapsed_seconds",
        "achieved_fps",
        "source_timeline_fps",
        "frames_skipped",
        "missed_deadlines",
        "mean_processing_ms",
        "max_processing_ms",
        "mean_frame_age_ms",
        "max_frame_age_ms",
        "crops_submitted",
        "crops_dropped",
        "stopped",
        "clock_synchronized",
    )
    outcome_keys = (
        "beans_with_jobs",
        "jobs_completed",
        "jobs_dropped",
        "jobs_failed",
        "stereo_pairs_complete",
        "stereo_pairs_incomplete",
        "classification_decision_bases",
        "classification_complete_pools",
        "classification_deadline_fallbacks",
        "actuations_succeeded",
        "actuations_failed",
        "settled",
    )
    temperatures = report.get("system_telemetry", {}).get("temperature_c", {})
    maximum_temperature = max(
        (
            value
            for sensor in temperatures.values()
            if isinstance(sensor, dict)
            and (value := _finite_value(sensor.get("max")))
            and math.isfinite(value)
        ),
        default=math.nan,
    )
    full_summary = report.get("summaries", {}).get("full", {})
    return {
        "source_report": str(resolved),
        "report_schema": report.get("schema"),
        "summary": {key: source_summary.get(key) for key in summary_keys},
        "outcome": {key: outcome.get(key) for key in outcome_keys},
        "acceptance_passed": full_summary.get("passed"),
        "maximum_temperature_c": maximum_temperature,
        "thermal_abort": report.get("system_telemetry", {}).get("thermal_abort"),
        "max_rss_mib": report.get("system_telemetry", {}).get("max_rss_mib"),
    }


def _write_live_charts(
    path: Path,
    beans: Sequence[Mapping[str, Any]],
    dark_summary: Mapping[str, Any],
) -> None:
    _appearance_chart(path / "appearance-distributions.png", beans)
    _size_chart(path / "size-and-volume.png", beans)
    _agreement_chart(path / "view-agreement.png", beans)
    _dark_chart(path / "dark-bean-candidates.png", beans, dark_summary)


def _appearance_chart(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    canvas = _chart_canvas(
        "Approximate two-view bean appearance",
        "Global calibration applied to retained linear sensor-BGR means; not per-pixel calibrated Lab",
    )
    left = _panel(canvas, (45, 125, 690, 710), "Lightness distribution")
    right = _panel(canvas, (765, 125, 690, 710), "Approximate Lab colour plane")
    _histogram(
        canvas,
        left,
        _bean_values(beans, "combined_approx_lab_l_mean"),
        (186, 126, 52),
        "Combined approximate Lab L*",
        "Confirmed bean count",
    )
    _scatter(
        canvas,
        right,
        _bean_values(beans, "combined_approx_lab_a_mean"),
        _bean_values(beans, "combined_approx_lab_b_mean"),
        [_bean_colour(bean) for bean in beans],
        "Approximate Lab a* (green to red)",
        "Approximate Lab b* (blue to yellow)",
    )
    _save_chart(path, canvas)


def _size_chart(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    canvas = _chart_canvas(
        "Projected size and approximate volume",
        "Pixel-domain proxies only; opposing views do not independently measure hidden thickness",
    )
    left = _panel(canvas, (45, 125, 690, 710), "Equivalent-sphere proxy")
    right = _panel(canvas, (765, 125, 690, 710), "Projected area agreement")
    _histogram(
        canvas,
        left,
        _bean_values(beans, "equivalent_sphere_volume_proxy_px3_median"),
        (70, 145, 210),
        "Equivalent-sphere volume proxy (pixel^3)",
        "Confirmed bean count",
    )
    _scatter(
        canvas,
        right,
        _bean_values(beans, "caml_area_native_px_median"),
        _bean_values(beans, "camr_area_native_px_median"),
        [(90, 120, 210)] * len(beans),
        "CamL projected area (native pixel^2)",
        "CamR projected area (native pixel^2)",
    )
    _save_chart(path, canvas)


def _agreement_chart(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    canvas = _chart_canvas(
        "Stereo-view agreement",
        "Large differences can indicate pose, segmentation error, or departure from the calibrated plane",
    )
    left = _panel(canvas, (45, 125, 690, 710), "CamR / CamL area ratio")
    right = _panel(canvas, (765, 125, 690, 710), "View lightness difference")
    _histogram(
        canvas,
        left,
        _bean_values(beans, "projected_area_ratio_camr_to_caml_median"),
        (92, 168, 105),
        "CamR area / CamL area (ratio)",
        "Confirmed bean count",
    )
    _histogram(
        canvas,
        right,
        _bean_values(beans, "approx_lab_l_view_delta_median"),
        (184, 105, 152),
        "CamR approximate L* - CamL approximate L*",
        "Confirmed bean count",
    )
    _save_chart(path, canvas)


def _dark_chart(
    path: Path,
    beans: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> None:
    mean = float(summary["lightness_mean"])
    standard_deviation = float(summary["lightness_sample_standard_deviation"])
    threshold = float(summary["threshold_mean_minus_2sd"])
    candidates = sum(bool(bean["dark_candidate_2sd"]) for bean in beans)
    canvas = _chart_canvas(
        "Dark-bean review candidates: one-sided 2-SD lightness screen",
        (
            f"Approximate L* mean={mean:.2f}, SD={standard_deviation:.2f}, "
            f"threshold={threshold:.2f}; {candidates}/{len(beans)} candidates"
        ),
    )
    left = _panel(canvas, (45, 125, 690, 710), "Lightness threshold")
    right = _panel(
        canvas,
        (765, 125, 690, 710),
        "Lab plane: grey=batch; colour=dark candidates",
    )
    _histogram(
        canvas,
        left,
        _bean_values(beans, "combined_approx_lab_l_mean"),
        (186, 126, 52),
        "Combined approximate Lab L*",
        "Confirmed bean count",
        vertical_markers=(
            (mean, (30, 120, 210), "batch mean"),
            (threshold, (35, 35, 35), "mean - 2 SD"),
        ),
        highlight_below=threshold,
    )
    ordered = sorted(beans, key=lambda bean: bool(bean["dark_candidate_2sd"]))
    colours = [
        _bean_colour(bean) if bean["dark_candidate_2sd"] else (205, 205, 205)
        for bean in ordered
    ]
    _scatter(
        canvas,
        right,
        _bean_values(ordered, "combined_approx_lab_a_mean"),
        _bean_values(ordered, "combined_approx_lab_b_mean"),
        colours,
        "Approximate Lab a* (green to red)",
        "Approximate Lab b* (blue to yellow)",
    )
    _save_chart(path, canvas)


def _bean_values(beans: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [_finite_value(bean.get(key)) for bean in beans]


def _bean_colour(bean: Mapping[str, Any]) -> tuple[int, int, int]:
    values = []
    for channel in ("b", "g", "r"):
        values.append(
            _mean_finite(
                bean.get(f"caml_approx_calibrated_mean_{channel}_median"),
                bean.get(f"camr_approx_calibrated_mean_{channel}_median"),
            )
        )
    if not all(math.isfinite(value) for value in values):
        return (180, 180, 180)
    return tuple(int(np.clip(value, 0, 255)) for value in values)


def _save_chart(path: Path, canvas: np.ndarray) -> None:
    if not cv2.imwrite(str(path), canvas):
        raise OSError(f"could not write chart: {path}")


def _median(rows: Sequence[Mapping[str, Any]], key: str) -> float:
    values = [_finite_value(row.get(key)) for row in rows]
    finite = [value for value in values if math.isfinite(value)]
    return float(np.median(finite)) if finite else math.nan


def _mean_finite(*values: object) -> float:
    finite = [value for item in values if math.isfinite(value := _finite_value(item))]
    return float(np.mean(finite)) if finite else math.nan


def _difference(right: object, left: object) -> float:
    right_value = _finite_value(right)
    left_value = _finite_value(left)
    return (
        right_value - left_value
        if math.isfinite(right_value) and math.isfinite(left_value)
        else math.nan
    )


def _positive_values(*values: object) -> list[float]:
    return [
        result
        for value in values
        if math.isfinite(result := _finite_value(value)) and result > 0
    ]


def _one_or_geometric_mean(values: Sequence[float]) -> float:
    return math.sqrt(values[0] * values[1]) if len(values) >= 2 else values[0]


def _finite_value(value: object, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _definitions() -> dict[str, str]:
    return {
        "approximate_calibrated_colour": (
            "Global dark subtraction, camera white balance, colour matrix and "
            "sRGB transfer applied to each retained masked sensor-BGR channel "
            "mean, with Lab calculated before display-channel rounding. This "
            "cannot reproduce spatial flat-field/defect correction "
            "or a mean of per-pixel nonlinear Lab values."
        ),
        "dark_candidate_2sd": (
            "One-sided provisional screen: combined approximate Lab L* at or "
            "below this batch's mean minus two sample standard deviations."
        ),
        "dark_candidate_robust": (
            "Companion robust screen using median minus two MAD-equivalent "
            "standard deviations. Retained for comparison, not sorting."
        ),
        "pixel_volume_proxy": (
            "A relative projected-size statistic in pixel^3, not calibrated "
            "physical volume. It uses the geometric mean of two views when "
            "available and the sole view for a one-view fallback. Opposing "
            "views do not observe hidden thickness."
        ),
        "classification_limit": (
            "Candidate flags rank items for review. A labelled known-good and "
            "dark-bean calibration set is required before choosing a sorting "
            "threshold or estimating false-reject rate."
        ),
    }


def _bundle_readme(source_name: str) -> str:
    return f"""# BeanoFlight Live Statistics Bundle

Source numerical capture: `{source_name}`

Start with:

- `dashboard/index.html` — interactive, offline batch explorer
- `charts/appearance-distributions.png`
- `charts/size-and-volume.png`
- `charts/view-agreement.png`
- `charts/dark-bean-candidates.png`
- `dark-bean-candidates.csv`

Every chart labels both axes and uses fine grid lines. `beans.csv` contains one
row per confirmed bean; `observations.csv` retains the one or two measurements
used to form it.

The colour values are **approximate calibrated mean colours** reconstructed
from the numerical linear sensor-BGR aggregates retained by the live pipeline.
They apply global dark, white-balance and colour-matrix terms, but cannot
reconstruct per-pixel flat-field/defect correction or exact Lab distributions.
Lab is calculated in floating point before RGB display-channel rounding.

The dark-bean flag is a provisional, one-sided within-batch screen at
`mean approximate L* - 2 sample SD`. It is useful for visualising candidates,
not yet for automatic rejection. Calibrate it using labelled known-good and
dark beans, and validate its false-reject rate before sorting with it.

Pixel volume values are relative proxies, not physical volume measurements.

The dashboard has no network dependencies and can be opened directly from a
local disk or Samba share. Selecting a chart region identifies the exact beans
behind it and offers CSV or colour-swatch contact-sheet export. Selections can
also be accumulated into a named, deduplicated review collection for later
threshold development. Stereo charts show paired CamL/CamR swatches. Live
captures retain no bean images, so all swatches show approximate mean colour
rather than texture.
"""


if __name__ == "__main__":
    main()
