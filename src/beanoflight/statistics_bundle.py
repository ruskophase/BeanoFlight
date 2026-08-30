"""Offline calibrated colour, silhouette and apparent-volume statistics."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .analysis import AnalysisEngine
from .background import parse_background_frame_indices
from .calibration import MetricPlaneCalibration, find_pinkplane_homography
from .detection import DetectorSettings, RawGreenDetector
from .models import BeanRef, TrackStatus
from .prediction import GateLayout
from .source import MMapRawVideoSource, SourceError
from .statistics_features import (
    component_crop_mask,
    extract_view_features,
    foreground_mask,
    local_area_scale,
    numeric_summary,
    paired_features,
    robust_median,
)
from .tracking import TrackerSettings

SCHEMA = "beanoflight-statistics-bundle/v1"
OBSERVATION_SCHEMA = "beanoflight-statistics-observation/v1"
BEAN_SCHEMA = "beanoflight-statistics-bean/v1"
DEFAULT_BACKGROUND_INDICES = (2, 8, 14)
VIEW_FEATURE_KEYS = (
    "area_px",
    "contour_area_px",
    "perimeter_px",
    "bbox_width_px",
    "bbox_height_px",
    "solidity",
    "extent",
    "circularity",
    "equivalent_diameter_px",
    "ellipse_minor_px",
    "ellipse_major_px",
    "ellipse_aspect_ratio",
    "ellipse_orientation_deg",
    "mean_b",
    "mean_g",
    "mean_r",
    "median_b",
    "median_g",
    "median_r",
    "luminance_mean",
    "luminance_p10",
    "luminance_median",
    "luminance_p90",
    "luminance_std",
    "lab_l_mean",
    "lab_l_median",
    "lab_a_mean",
    "lab_b_mean",
    "lab_chroma_mean",
    "hsv_hue_mean_deg",
    "hsv_saturation_mean",
    "linear_red_chromaticity",
    "linear_green_chromaticity",
    "linear_blue_chromaticity",
    "highlight_fraction",
    "shadow_fraction",
    "colour_pixel_count",
    "area_mm2",
    "equivalent_diameter_mm",
    "ellipse_minor_mm",
    "ellipse_major_mm",
)
PAIRED_FEATURE_KEYS = (
    "projected_area_geomean_mm2",
    "projected_area_ratio_camr_to_caml",
    "equivalent_sphere_volume_proxy_mm3",
    "rotational_ellipsoid_volume_proxy_mm3",
    "lab_l_view_delta",
    "lab_a_view_delta",
    "lab_b_view_delta",
    "refinement_distance_px",
)


@dataclass(frozen=True, slots=True)
class BundleSettings:
    background_indices: tuple[int, ...] = DEFAULT_BACKGROUND_INDICES
    crop_size_px: int = 320
    samples_per_bean: int = 3
    maximum_frames: int | None = None
    progress_every: int = 600

    def validate(self) -> None:
        if not self.background_indices:
            raise ValueError("at least one background frame is required")
        if self.crop_size_px < 64 or self.crop_size_px % 2:
            raise ValueError("crop size must be an even integer of at least 64")
        if not 1 <= self.samples_per_bean <= 3:
            raise ValueError("samples per bean must be between one and three")
        if self.maximum_frames is not None and self.maximum_frames <= 0:
            raise ValueError("maximum frames must be positive")
        if self.progress_every <= 0:
            raise ValueError("progress interval must be positive")


def _background_indices(value: str) -> tuple[int, ...]:
    try:
        return parse_background_frame_indices(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beano-statistics",
        description=(
            "Create self-contained calibrated stereo colour, silhouette and "
            "apparent-volume Statistics Bundles from seekable FastCap recordings."
        ),
    )
    result.add_argument("recordings", type=Path, nargs="+")
    result.add_argument(
        "--background-frames",
        type=_background_indices,
        default=DEFAULT_BACKGROUND_INDICES,
        metavar="I0,I1,I2",
        help="human-confirmed empty zero-based frames (default: 2,8,14)",
    )
    result.add_argument("--homography", type=Path)
    result.add_argument("--output-root", type=Path)
    result.add_argument("--crop-size", type=int, default=320)
    result.add_argument("--samples-per-bean", type=int, default=3)
    result.add_argument("--maximum-frames", type=int)
    result.add_argument("--progress-every", type=int, default=600)
    result.add_argument("--overwrite", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    settings = BundleSettings(
        background_indices=arguments.background_frames,
        crop_size_px=arguments.crop_size,
        samples_per_bean=arguments.samples_per_bean,
        maximum_frames=arguments.maximum_frames,
        progress_every=arguments.progress_every,
    )
    try:
        settings.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    outputs: list[dict[str, object]] = []
    for recording in arguments.recordings:
        resolved = recording.expanduser().resolve()
        output = (
            arguments.output_root.expanduser().resolve()
            / f"{resolved.name}-statistics"
            if arguments.output_root is not None
            else resolved / "postprocess/statistics-bundle"
        )
        outputs.append(
            build_statistics_bundle(
                resolved,
                output,
                settings=settings,
                homography_path=arguments.homography,
                overwrite=arguments.overwrite,
                progress=lambda completed, total, samples, _name=resolved.name: print(
                    f"{_name}: frame {completed}/{total}, "
                    f"calibrated stereo samples {samples}",
                    flush=True,
                ),
            )
        )
    print(json.dumps(outputs, indent=2), flush=True)


def build_statistics_bundle(
    recording: Path,
    output: Path,
    *,
    settings: BundleSettings | None = None,
    homography_path: Path | None = None,
    overwrite: bool = False,
    progress=None,
) -> dict[str, object]:
    """Run the production tracker and write an atomic Statistics Bundle."""

    options = settings or BundleSettings()
    options.validate()
    recording = recording.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"Statistics Bundle already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.tmp-{os.getpid()}"
    if temporary.exists():
        raise FileExistsError(f"temporary bundle path already exists: {temporary}")
    temporary.mkdir(parents=True)

    source: MMapRawVideoSource | None = None
    crop_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="statistics-camr"
    )
    started = time.perf_counter_ns()
    observations: list[dict[str, Any]] = []
    track_info: dict[BeanRef, dict[str, Any]] = {}
    samples_by_ref: dict[BeanRef, list[dict[str, Any]]] = defaultdict(list)
    bands_by_ref: dict[BeanRef, set[int]] = defaultdict(set)
    representatives: dict[BeanRef, bytes] = {}
    feature_times: list[float] = []
    feature_cpu_times: list[float] = []
    materialization_times: list[float] = []
    sampling_failures: dict[str, int] = defaultdict(int)
    frames_processed = 0
    detector_settings = DetectorSettings()
    right_mask_settings = detector_settings.updated(
        blur_kernel=5,
        threshold=22,
        close_kernel=5,
        close_iterations=1,
        open_kernel=3,
        open_iterations=1,
        dilate_iterations=0,
    )
    try:
        source = MMapRawVideoSource(recording, crop_processing="calibrated")
        invalid = tuple(
            index
            for index in options.background_indices
            if not 0 <= index < source.metadata.frame_count
        )
        if invalid:
            raise SourceError(f"background frames outside recording: {invalid}")
        homography = (
            homography_path.expanduser().resolve()
            if homography_path is not None
            else find_pinkplane_homography(recording)
        )
        if homography is None:
            raise SourceError("could not locate a PinkPlane homography")
        source.configure_stereo(homography, options.background_indices)
        background = source.build_background(options.background_indices)
        calibration = MetricPlaneCalibration.from_pinkplane(
            homography,
            image_size_px=(source.metadata.width, source.metadata.height),
        )

        def positions_mapper(points):
            return calibration.pixels_to_mm(source.undistort_points(points))

        engine = AnalysisEngine(
            calibration,
            RawGreenDetector(detector_settings),
            background,
            tracker_settings=TrackerSettings(),
            gate_layout=GateLayout(calibration.sorting_line_y()),
            positions_mapper=positions_mapper,
        )
        total = min(
            source.metadata.frame_count,
            options.maximum_frames or source.metadata.frame_count,
        )
        right_background = source.right_background_gray()
        right_fallback_background = source.right_background_gray(dual_green=True)
        fov_height_mm = calibration.bottom_y_mm - calibration.top_y_mm
        _warm_feature_kernel()

        def caml_to_mm(point):
            return calibration.pixel_to_mm(source.undistort_point(point))

        def camr_to_mm(point):
            caml_point = source.stereo_calibration.project_distorted_camr_to_undistorted_caml(
                point
            )
            return calibration.pixel_to_mm(caml_point)

        for frame_index in range(total):
            frame = source.frame(frame_index)
            try:
                timestamp_ns = source.timestamp_ns(frame_index)
                analysis = engine.process(frame, frame_index, timestamp_ns)
                frames_processed += 1
                candidates = []
                for track in analysis.tracks:
                    info = track_info.setdefault(
                        track.bean_ref,
                        {
                            "first_frame_index": frame_index,
                            "last_frame_index": frame_index,
                            "first_timestamp_ns": timestamp_ns,
                            "last_timestamp_ns": timestamp_ns,
                            "maximum_hits": 0,
                            "confirmed": False,
                            "terminal_status": track.status.value,
                        },
                    )
                    info["last_frame_index"] = frame_index
                    info["last_timestamp_ns"] = timestamp_ns
                    info["maximum_hits"] = max(int(info["maximum_hits"]), track.hits)
                    info["confirmed"] = bool(info["confirmed"]) or track.status in {
                        TrackStatus.CONFIRMED,
                        TrackStatus.OCCLUDED,
                        TrackStatus.EXITED,
                    }
                    info["terminal_status"] = track.status.value
                    if not track.history or len(samples_by_ref[track.bean_ref]) >= options.samples_per_bean:
                        continue
                    observation = track.history[-1]
                    if observation.frame_index != frame_index:
                        continue
                    normalized = (observation.position_mm[1] - calibration.top_y_mm) / fov_height_mm
                    band = min(2, max(0, int(normalized * 3.0)))
                    if band in bands_by_ref[track.bean_ref]:
                        continue
                    candidates.append((track, observation, band))

                left_foreground = None
                right_foreground = None
                right_fallback_foreground = None
                for track, observation, band in candidates:
                    prepared = source.prepare_stereo_crop(
                        frame,
                        observation.detection.centroid_px,
                        options.crop_size_px,
                        allow_padding=False,
                        allow_resize=True,
                    )
                    if prepared is None:
                        sampling_failures["stereo_crop_unavailable"] += 1
                        continue
                    if left_foreground is None:
                        left_foreground = foreground_mask(
                            frame.detection_gray, background, detector_settings
                        )
                    left_mask = component_crop_mask(
                        left_foreground,
                        prepared.pair.caml_centroid_px,
                        prepared.source_size_px,
                        maximum_distance_px=24.0,
                    )
                    if right_foreground is None:
                        right_foreground = foreground_mask(
                            source.right_detection_gray(frame),
                            right_background,
                            right_mask_settings,
                        )
                    right_mask = component_crop_mask(
                        right_foreground,
                        prepared.pair.camr_centroid_px,
                        prepared.source_size_px,
                        maximum_distance_px=24.0,
                    )
                    mask_domain = "single-green"
                    if right_mask is None:
                        if right_fallback_foreground is None:
                            right_fallback_foreground = foreground_mask(
                                source.right_detection_gray(frame, dual_green=True),
                                right_fallback_background,
                                right_mask_settings,
                            )
                        right_mask = component_crop_mask(
                            right_fallback_foreground,
                            prepared.pair.camr_centroid_px,
                            prepared.source_size_px,
                            maximum_distance_px=24.0,
                        )
                        mask_domain = "dual-green-fallback"
                    if left_mask is None or right_mask is None:
                        sampling_failures["component_mask_unavailable"] += 1
                        continue
                    materialization_started = time.perf_counter_ns()
                    right_image_future = crop_executor.submit(
                        prepared.camr_materializer
                    )
                    try:
                        left_image = prepared.caml_materializer()
                        right_image = right_image_future.result()
                    except Exception:
                        right_image_future.cancel()
                        raise
                    materialization_ms = (
                        time.perf_counter_ns() - materialization_started
                    ) / 1_000_000.0
                    try:
                        left_scale = local_area_scale(
                            prepared.pair.caml_centroid_px, caml_to_mm
                        )
                        right_scale = local_area_scale(
                            prepared.pair.camr_centroid_px, camr_to_mm
                        )
                        feature_started = time.perf_counter_ns()
                        right_features_future = crop_executor.submit(
                            extract_view_features,
                            right_image,
                            right_mask,
                            area_scale_mm2_per_px=right_scale,
                        )
                        try:
                            left_features = extract_view_features(
                                left_image,
                                left_mask,
                                area_scale_mm2_per_px=left_scale,
                            )
                            right_features = right_features_future.result()
                        except Exception:
                            right_features_future.cancel()
                            raise
                    except ValueError:
                        sampling_failures["invalid_or_clipped_silhouette"] += 1
                        continue
                    kernel_ms = (
                        time.perf_counter_ns() - feature_started
                    ) / 1_000_000.0
                    kernel_cpu_ms = (
                        left_features.kernel_ms + right_features.kernel_ms
                    )
                    row: dict[str, Any] = {
                        "schema": OBSERVATION_SCHEMA,
                        "bean_id": str(track.bean_ref),
                        "bean_sequence": track.bean_ref.sequence,
                        "sample_index": len(samples_by_ref[track.bean_ref]) + 1,
                        "fov_band": ("top", "middle", "bottom")[band],
                        "frame_index": frame_index,
                        "timestamp_ns": timestamp_ns,
                        "track_status": track.status.value,
                        "track_hits": track.hits,
                        "caml_centroid_x_px": prepared.pair.caml_centroid_px[0],
                        "caml_centroid_y_px": prepared.pair.caml_centroid_px[1],
                        "camr_centroid_x_px": prepared.pair.camr_centroid_px[0],
                        "camr_centroid_y_px": prepared.pair.camr_centroid_px[1],
                        "camr_projected_x_px": prepared.pair.camr_projected_centroid_px[0],
                        "camr_projected_y_px": prepared.pair.camr_projected_centroid_px[1],
                        "right_frame_index": prepared.pair.right_frame_index,
                        "synchronization_delta_ns": prepared.pair.synchronization_delta_ns,
                        "source_crop_size_px": prepared.source_size_px,
                        "camr_mask_domain": mask_domain,
                        "materialization_ms": materialization_ms,
                        "feature_kernel_ms": kernel_ms,
                        "feature_kernel_cpu_ms": kernel_cpu_ms,
                        "refinement_distance_px": prepared.pair.refinement_distance_px,
                    }
                    row.update({f"caml_{key}": value for key, value in left_features.values.items()})
                    row.update({f"camr_{key}": value for key, value in right_features.values.items()})
                    row.update(paired_features(left_features.values, right_features.values, prepared.pair.refinement_distance_px))
                    observations.append(row)
                    samples_by_ref[track.bean_ref].append(row)
                    bands_by_ref[track.bean_ref].add(band)
                    feature_times.append(kernel_ms)
                    feature_cpu_times.append(kernel_cpu_ms)
                    materialization_times.append(materialization_ms)
                    if track.bean_ref not in representatives or band == 1:
                        representatives[track.bean_ref] = _representative_jpeg(
                            left_image, right_image, left_mask, right_mask
                        )
            finally:
                source.release_frame(frame)
            if progress is not None and (
                frame_index == 0
                or (frame_index + 1) % options.progress_every == 0
                or frame_index + 1 == total
            ):
                progress(frame_index + 1, total, len(observations))

        if frames_processed:
            boundary_timestamp = source.timestamp_ns(frames_processed - 1)
            for track in engine.tracker.cancel_active_at_boundary(boundary_timestamp):
                info = track_info.get(track.bean_ref)
                if info is not None:
                    info["terminal_status"] = track.status.value

        confirmed = {ref for ref, info in track_info.items() if info["confirmed"]}
        confirmed_ids = {str(ref) for ref in confirmed}
        observations = [
            row for row in observations if str(row["bean_id"]) in confirmed_ids
        ]
        # Rebuild sample groups after tentative one-hit tracks have been removed.
        confirmed_samples: dict[BeanRef, list[dict[str, Any]]] = {
            ref: rows for ref, rows in samples_by_ref.items() if ref in confirmed and rows
        }
        beans = _aggregate_beans(confirmed_samples, track_info)
        _score_appearance_outliers(beans)

        charts = temporary / "charts"
        outliers = temporary / "outliers"
        charts.mkdir()
        outliers.mkdir()
        _write_csv(temporary / "observations.csv", observations)
        _write_csv(temporary / "beans.csv", beans)
        _write_jsonl(temporary / "observations.jsonl", observations)
        _write_jsonl(temporary / "beans.jsonl", beans)
        _write_charts(charts, beans)
        _write_outliers(outliers, beans, representatives, confirmed_samples)

        elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000.0
        summary = _build_summary(
            recording,
            beans,
            observations,
            track_info,
            confirmed,
            feature_times,
            feature_cpu_times,
            materialization_times,
            frames_processed,
            elapsed_seconds,
            sampling_failures,
            source.stereo_statistics(),
        )
        _write_json(temporary / "summary.json", summary)
        (temporary / "README.md").write_text(
            _bundle_readme(recording.name), encoding="utf-8"
        )
        provenance = _provenance(
            recording,
            homography,
            source,
            calibration,
            options,
            frames_processed,
        )
        files = _file_inventory(temporary)
        manifest = {
            "schema": SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "recording": str(recording),
            "provenance": provenance,
            "schemas": {
                "observations": OBSERVATION_SCHEMA,
                "beans": BEAN_SCHEMA,
            },
            "definitions": _definitions(),
            "summary": summary,
            "files": files,
        }
        _write_json(temporary / "manifest.json", manifest)
        if output.exists():
            shutil.rmtree(output)
        temporary.replace(output)
        return {
            "recording": str(recording),
            "output": str(output),
            "confirmed_beans": len(beans),
            "stereo_samples": len(observations),
            "frames_processed": frames_processed,
            "elapsed_seconds": elapsed_seconds,
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        crop_executor.shutdown(wait=True, cancel_futures=True)
        if source is not None:
            source.close()


def _paired_features(
    left: Mapping[str, float],
    right: Mapping[str, float],
    refinement_distance_px: float,
) -> dict[str, float]:
    """Compatibility alias retained for existing prototype consumers."""

    return paired_features(left, right, refinement_distance_px)


def _aggregate_beans(
    groups: Mapping[BeanRef, Sequence[Mapping[str, Any]]],
    track_info: Mapping[BeanRef, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ref in sorted(groups):
        rows = groups[ref]
        info = track_info[ref]
        bean: dict[str, Any] = {
            "schema": BEAN_SCHEMA,
            "bean_id": str(ref),
            "bean_sequence": ref.sequence,
            "sample_count": len(rows),
            "sampled_fov_bands": ";".join(str(row["fov_band"]) for row in rows),
            "first_frame_index": info["first_frame_index"],
            "last_frame_index": info["last_frame_index"],
            "first_timestamp_ns": info["first_timestamp_ns"],
            "last_timestamp_ns": info["last_timestamp_ns"],
            "track_maximum_hits": info["maximum_hits"],
            "terminal_status": info["terminal_status"],
        }
        for camera in ("caml", "camr"):
            for key in VIEW_FEATURE_KEYS:
                bean[f"{camera}_{key}_median"] = robust_median(rows, f"{camera}_{key}")
        for key in PAIRED_FEATURE_KEYS:
            bean[f"{key}_median"] = robust_median(rows, key)
        bean["projected_area_geomean_mm2_p10"] = _percentile(rows, "projected_area_geomean_mm2", 10)
        bean["projected_area_geomean_mm2_p90"] = _percentile(rows, "projected_area_geomean_mm2", 90)
        bean["combined_lab_l_mean"] = _mean_pair(bean, "lab_l_mean")
        bean["combined_lab_a_mean"] = _mean_pair(bean, "lab_a_mean")
        bean["combined_lab_b_mean"] = _mean_pair(bean, "lab_b_mean")
        bean["combined_lab_chroma_mean"] = _mean_pair(bean, "lab_chroma_mean")
        bean["combined_saturation_mean"] = _mean_pair(bean, "hsv_saturation_mean")
        bean["combined_red_chromaticity"] = _mean_pair(bean, "linear_red_chromaticity")
        bean["combined_green_chromaticity"] = _mean_pair(bean, "linear_green_chromaticity")
        bean["combined_blue_chromaticity"] = _mean_pair(bean, "linear_blue_chromaticity")
        result.append(bean)
    return result


def _mean_pair(bean: Mapping[str, Any], key: str) -> float:
    values = np.asarray(
        (bean[f"caml_{key}_median"], bean[f"camr_{key}_median"]), np.float64
    )
    finite = values[np.isfinite(values)]
    return float(np.mean(finite)) if finite.size else math.nan


def _percentile(rows: Sequence[Mapping[str, Any]], key: str, percentile: float) -> float:
    values = [float(row[key]) for row in rows if key in row and math.isfinite(float(row[key]))]
    return float(np.percentile(values, percentile)) if values else math.nan


def _score_appearance_outliers(beans: list[dict[str, Any]]) -> None:
    if not beans:
        return
    keys = (
        "combined_lab_l_mean",
        "combined_lab_a_mean",
        "combined_lab_b_mean",
        "combined_saturation_mean",
        "combined_red_chromaticity",
        "combined_green_chromaticity",
        "combined_blue_chromaticity",
    )
    values = np.asarray([[float(bean[key]) for key in keys] for bean in beans])
    median = np.nanmedian(values, axis=0)
    mad = np.nanmedian(np.abs(values - median), axis=0) * 1.4826
    floors = np.asarray((3.0, 2.0, 2.0, 0.03, 0.015, 0.015, 0.015))
    scale = np.maximum(mad, floors)
    scores = np.sqrt(np.nanmean(np.square((values - median) / scale), axis=1))
    order = np.argsort(scores)
    percentiles = np.empty_like(scores)
    percentiles[order] = (np.arange(len(scores)) + 1) / len(scores) * 100.0
    for bean, score, percentile in zip(beans, scores, percentiles):
        lightness = float(bean["combined_lab_l_mean"])
        a_star = float(bean["combined_lab_a_mean"])
        b_star = float(bean["combined_lab_b_mean"])
        chroma = float(bean["combined_lab_chroma_mean"])
        flags = []
        if lightness >= 70.0 and chroma <= 12.0:
            flags.append("light-low-chroma/silver-candidate")
        if b_star >= 20.0 and a_star >= 4.0:
            flags.append("yellow-orange-candidate")
        if lightness <= 25.0:
            flags.append("dark-candidate")
        if score >= 4.0 or percentile >= 99.0:
            flags.append("appearance-outlier")
        bean["appearance_outlier_score"] = float(score)
        bean["appearance_outlier_percentile"] = float(percentile)
        bean["appearance_flags"] = ";".join(flags)


def _build_summary(
    recording: Path,
    beans: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    track_info: Mapping[BeanRef, Mapping[str, Any]],
    confirmed: set[BeanRef],
    feature_times: Sequence[float],
    feature_cpu_times: Sequence[float],
    materialization_times: Sequence[float],
    frames_processed: int,
    elapsed_seconds: float,
    sampling_failures: Mapping[str, int],
    stereo_statistics: Mapping[str, object],
) -> dict[str, Any]:
    def bean_values(key: str) -> list[float]:
        return [float(bean[key]) for bean in beans if math.isfinite(float(bean[key]))]

    feature_summary = numeric_summary(feature_times)
    job_summary = numeric_summary(
        [
            float(row["materialization_ms"]) + float(row["feature_kernel_ms"])
            for row in observations
        ]
    )
    frame_workload = _per_frame_workload(observations, frames_processed)
    p95_ms = feature_summary["p95"]
    job_p95_ms = job_summary["p95"]
    return {
        "recording": str(recording),
        "counts": {
            "frames_processed": frames_processed,
            "public_tracks": len(track_info),
            "confirmed_tracks": len(confirmed),
            "confirmed_beans_with_statistics": len(beans),
            "calibrated_stereo_samples": len(observations),
            "feature_jobs_executed": len(feature_times),
            "discarded_tentative_track_samples": max(
                0, len(feature_times) - len(observations)
            ),
            "beans_without_valid_stereo_sample": len(confirmed) - len(beans),
        },
        "sampling_failures": dict(sorted(sampling_failures.items())),
        "distributions": {
            "combined_lab_l_mean": numeric_summary(bean_values("combined_lab_l_mean")),
            "combined_lab_a_mean": numeric_summary(bean_values("combined_lab_a_mean")),
            "combined_lab_b_mean": numeric_summary(bean_values("combined_lab_b_mean")),
            "projected_area_geomean_mm2": numeric_summary(bean_values("projected_area_geomean_mm2_median")),
            "equivalent_sphere_volume_proxy_mm3": numeric_summary(bean_values("equivalent_sphere_volume_proxy_mm3_median")),
            "rotational_ellipsoid_volume_proxy_mm3": numeric_summary(bean_values("rotational_ellipsoid_volume_proxy_mm3_median")),
            "appearance_outlier_score": numeric_summary(bean_values("appearance_outlier_score")),
        },
        "performance": {
            "elapsed_seconds": elapsed_seconds,
            "source_frames_per_second": frames_processed / max(elapsed_seconds, 1e-9),
            "stereo_samples_per_second_end_to_end": len(observations) / max(elapsed_seconds, 1e-9),
            "calibrated_crop_materialization_ms": numeric_summary(materialization_times),
            "two_view_feature_kernel_ms": feature_summary,
            "two_view_feature_kernel_cpu_ms": numeric_summary(feature_cpu_times),
            "calibrated_statistics_job_wall_ms": job_summary,
            "calibrated_statistics_job_capacity_at_p95_per_second": (
                1000.0 / float(job_p95_ms) if job_p95_ms is not None else None
            ),
            "feature_kernel_fraction_of_16_67ms_at_p95": (
                float(p95_ms) / (1000.0 / 60.0) if p95_ms is not None else None
            ),
            "sampled_frame_workload": frame_workload,
        },
        "stereo_localizer": dict(stereo_statistics),
    }


def _per_frame_workload(
    observations: Sequence[Mapping[str, Any]], frames_processed: int
) -> dict[str, Any]:
    sample_counts: dict[int, int] = defaultdict(int)
    kernel_wall_ms: dict[int, float] = defaultdict(float)
    extraction_wall_ms: dict[int, float] = defaultdict(float)
    for row in observations:
        frame_index = int(row["frame_index"])
        sample_counts[frame_index] += 1
        kernel = float(row["feature_kernel_ms"])
        kernel_wall_ms[frame_index] += kernel
        extraction_wall_ms[frame_index] += kernel + float(row["materialization_ms"])
    active_frames = sorted(sample_counts)
    all_counts = [sample_counts.get(index, 0) for index in range(frames_processed)]
    active_kernel = [kernel_wall_ms[index] for index in active_frames]
    active_extraction = [extraction_wall_ms[index] for index in active_frames]
    busiest = max(active_frames, key=lambda index: sample_counts[index], default=None)
    kernel_summary = numeric_summary(active_kernel)
    return {
        "active_sampled_frames": len(active_frames),
        "samples_per_source_frame": numeric_summary(all_counts),
        "samples_per_active_frame": numeric_summary(
            [sample_counts[index] for index in active_frames]
        ),
        "feature_kernel_wall_ms_per_active_frame": kernel_summary,
        "calibrated_extraction_wall_ms_per_active_frame": numeric_summary(
            active_extraction
        ),
        "feature_kernel_fraction_of_16_67ms_per_active_frame_at_p95": (
            float(kernel_summary["p95"]) / (1000.0 / 60.0)
            if kernel_summary["p95"] is not None
            else None
        ),
        "busiest_frame": (
            None
            if busiest is None
            else {
                "frame_index": busiest,
                "samples": sample_counts[busiest],
                "feature_kernel_wall_ms": kernel_wall_ms[busiest],
                "calibrated_extraction_wall_ms": extraction_wall_ms[busiest],
            }
        ),
    }


def _provenance(
    recording: Path,
    homography: Path,
    source: MMapRawVideoSource,
    calibration: MetricPlaneCalibration,
    settings: BundleSettings,
    frames_processed: int,
) -> dict[str, Any]:
    recording_json = recording / "recording.json"
    recording_payload = _read_json(recording_json)
    calibration_value = recording_payload.get("calibration", {})
    return {
        "recording_manifest": {
            "path": str(recording_json),
            "sha256": _sha256(recording_json),
            "schema": recording_payload.get("schema"),
            "classification": recording_payload.get("classification"),
            "test_override": recording_payload.get("plan", {}).get("test_override"),
        },
        "camera_tuner_bundle_id": calibration_value.get("bundle_id"),
        "homography": {
            "path": str(homography),
            "sha256": _sha256(homography),
            "coordinate_domain": "undistorted",
        },
        "metric_plane": calibration.to_json(),
        "source_pipeline": source.pipeline_metadata,
        "settings": {
            "background_frame_indices": list(settings.background_indices),
            "background_confirmation": "human-confirmed empty in prior review",
            "crop_processing": "calibrated",
            "crop_size_px": settings.crop_size_px,
            "maximum_samples_per_bean": settings.samples_per_bean,
            "sampling_bands": ["top", "middle", "bottom"],
            "feature_kernel_pre_warmed": True,
            "frames_processed": frames_processed,
        },
        "software": {
            "package": "beanoflight",
            "git_commit": _git_commit(),
            "opencv": cv2.__version__,
            "numpy": np.__version__,
        },
    }


def _definitions() -> dict[str, str]:
    return {
        "calibrated_colour": (
            "Camera-Tuner dark/flat/defect correction, white balance, colour "
            "matrix and sRGB transfer are applied independently to both views."
        ),
        "calibrated_statistics_job_wall_ms": (
            "Wall time for concurrent CamL/CamR calibrated RAW materialization "
            "followed by the concurrent two-view feature kernel. The normal "
            "live classifier uses ml-fast crops and does not already pay this "
            "calibrated materialization cost."
        ),
        "bean_colour": (
            "Colour statistics use an eroded foreground silhouette to reduce "
            "background and motion-edge contamination. CIE Lab values are the "
            "most useful camera-independent descriptors in this prototype."
        ),
        "apparent_size": (
            "Area and ellipse measurements describe the projected silhouette "
            "seen by each camera. Local homography Jacobians convert pixels to "
            "fall-plane mm units."
        ),
        "volume_proxy": (
            "Not a physical volume measurement. The sphere proxy assumes the "
            "geometric-mean projected area is circular; the ellipsoid proxy "
            "assumes rotational symmetry about the measured major axis. The two "
            "cameras are opposing rather than orthogonal, so hidden thickness is "
            "not independently observed."
        ),
        "appearance_outlier_score": (
            "Robust within-recording multivariate distance over two-view Lab, "
            "saturation and linear-RGB chromaticity. It ranks unusual objects but "
            "does not identify material without labelled examples."
        ),
    }


def _bundle_readme(recording_name: str) -> str:
    return f"""# BeanoFlight Statistics Bundle

Source recording: `{recording_name}`

Open `charts/appearance-distributions.png`, `charts/size-and-volume.png`,
`charts/view-agreement.png`, and `outliers/contact-sheet.png` first.

- `beans.csv` / `beans.jsonl`: robust per-track medians and outlier scores.
- `observations.csv` / `observations.jsonl`: calibrated stereo sample evidence.
- `summary.json`: batch distributions, coverage, failures and timing.
- `manifest.json`: schemas, exact calibration/provenance, definitions and hashes.

The two volume fields are explicitly **proxies**, not physical volume. Both
cameras view approximately opposite sides rather than orthogonal axes, so the
unseen thickness is assumed. Use weighed/measured reference objects before
turning either proxy into a calibrated volume estimate.
"""


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


def _csv_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(_json_safe(row), sort_keys=True) + "\n")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def _write_charts(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    _appearance_chart(path / "appearance-distributions.png", beans)
    _size_chart(path / "size-and-volume.png", beans)
    _agreement_chart(path / "view-agreement.png", beans)


def _chart_canvas(title: str, subtitle: str) -> np.ndarray:
    canvas = np.full((900, 1500, 3), 248, np.uint8)
    cv2.putText(canvas, title, (55, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.25, (25, 25, 25), 2, cv2.LINE_AA)
    cv2.putText(canvas, subtitle, (55, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (80, 80, 80), 1, cv2.LINE_AA)
    return canvas


def _panel(canvas: np.ndarray, rect: tuple[int, int, int, int], title: str) -> tuple[int, int, int, int]:
    x, y, width, height = rect
    cv2.rectangle(canvas, (x, y), (x + width, y + height), (218, 218, 218), 1)
    cv2.putText(canvas, title, (x + 18, y + 32), cv2.FONT_HERSHEY_SIMPLEX, 0.63, (35, 35, 35), 1, cv2.LINE_AA)
    return x + 58, y + 58, width - 82, height - 100


def _histogram(canvas: np.ndarray, rect: tuple[int, int, int, int], values: Sequence[float], colour: tuple[int, int, int], label: str) -> None:
    x, y, width, height = rect
    finite = np.asarray([value for value in values if math.isfinite(value)], np.float64)
    cv2.line(canvas, (x, y + height), (x + width, y + height), (100, 100, 100), 1)
    cv2.line(canvas, (x, y), (x, y + height), (100, 100, 100), 1)
    if finite.size:
        low, high = float(np.min(finite)), float(np.max(finite))
        if high <= low:
            high = low + 1.0
        counts, _edges = np.histogram(finite, bins=min(30, max(5, round(math.sqrt(len(finite))))), range=(low, high))
        bar_width = width / len(counts)
        maximum = max(int(np.max(counts)), 1)
        for index, count in enumerate(counts):
            left = round(x + index * bar_width)
            right = round(x + (index + 1) * bar_width) - 1
            top = round(y + height - height * int(count) / maximum)
            cv2.rectangle(canvas, (left, top), (max(left, right), y + height - 1), colour, -1)
        cv2.putText(canvas, f"{low:.2f}", (x, y + height + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (70, 70, 70), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"{high:.2f}", (x + width - 65, y + height + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (70, 70, 70), 1, cv2.LINE_AA)
        median = float(np.median(finite))
        cv2.putText(canvas, f"median {median:.2f}; n={len(finite)}", (x + 8, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (55, 55, 55), 1, cv2.LINE_AA)
    cv2.putText(canvas, label, (x + width // 2 - 70, y + height + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (55, 55, 55), 1, cv2.LINE_AA)


def _scatter(canvas: np.ndarray, rect: tuple[int, int, int, int], xs: Sequence[float], ys: Sequence[float], colours: Sequence[tuple[int, int, int]], x_label: str, y_label: str) -> None:
    x, y, width, height = rect
    points = [(a, b, colour) for a, b, colour in zip(xs, ys, colours) if math.isfinite(a) and math.isfinite(b)]
    cv2.line(canvas, (x, y + height), (x + width, y + height), (100, 100, 100), 1)
    cv2.line(canvas, (x, y), (x, y + height), (100, 100, 100), 1)
    if points:
        x_values = np.asarray([item[0] for item in points])
        y_values = np.asarray([item[1] for item in points])
        x_low, x_high = _plot_limits(x_values)
        y_low, y_high = _plot_limits(y_values)
        for x_value, y_value, colour in points:
            px = round(x + (x_value - x_low) / (x_high - x_low) * width)
            py = round(y + height - (y_value - y_low) / (y_high - y_low) * height)
            cv2.circle(canvas, (px, py), 3, colour, -1, cv2.LINE_AA)
        cv2.putText(canvas, f"{x_low:.2f}", (x, y + height + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (70, 70, 70), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"{x_high:.2f}", (x + width - 62, y + height + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (70, 70, 70), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"{y_low:.2f}", (x - 50, y + height), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (70, 70, 70), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"{y_high:.2f}", (x - 50, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (70, 70, 70), 1, cv2.LINE_AA)
    cv2.putText(canvas, x_label, (x + width // 2 - 65, y + height + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (55, 55, 55), 1, cv2.LINE_AA)
    cv2.putText(canvas, y_label, (x + 8, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (55, 55, 55), 1, cv2.LINE_AA)


def _plot_limits(values: np.ndarray) -> tuple[float, float]:
    low, high = float(np.min(values)), float(np.max(values))
    if high <= low:
        return low - 0.5, high + 0.5
    margin = (high - low) * 0.05
    return low - margin, high + margin


def _appearance_chart(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    canvas = _chart_canvas("Calibrated two-view bean appearance", "Each point is one confirmed track; colours are display approximations of calibrated mean RGB")
    left = _panel(canvas, (45, 125, 690, 710), "Lightness distribution")
    right = _panel(canvas, (765, 125, 690, 710), "CIE Lab colour plane")
    _histogram(canvas, left, [float(bean["combined_lab_l_mean"]) for bean in beans], (186, 126, 52), "combined Lab L*")
    colours = [_bean_colour(bean) for bean in beans]
    _scatter(canvas, right, [float(bean["combined_lab_a_mean"]) for bean in beans], [float(bean["combined_lab_b_mean"]) for bean in beans], colours, "Lab a* (green - red)", "Lab b* (blue - yellow)")
    cv2.imwrite(str(path), canvas)


def _size_chart(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    canvas = _chart_canvas("Projected size and approximate volume", "Volume values are proxies; opposing views do not independently measure hidden thickness")
    left = _panel(canvas, (45, 125, 690, 710), "Rotational-ellipsoid proxy")
    right = _panel(canvas, (765, 125, 690, 710), "Projected area agreement")
    _histogram(canvas, left, [float(bean["rotational_ellipsoid_volume_proxy_mm3_median"]) for bean in beans], (70, 145, 210), "volume proxy (mm^3)")
    _scatter(canvas, right, [float(bean["caml_area_mm2_median"]) for bean in beans], [float(bean["camr_area_mm2_median"]) for bean in beans], [(90, 120, 210)] * len(beans), "CamL area (mm^2)", "CamR area (mm^2)")
    cv2.imwrite(str(path), canvas)


def _agreement_chart(path: Path, beans: Sequence[Mapping[str, Any]]) -> None:
    canvas = _chart_canvas("Stereo-view agreement", "Large differences can indicate pose, silhouette mismatch, segmentation error, or departure from the calibrated plane")
    left = _panel(canvas, (45, 125, 690, 710), "CamR / CamL projected-area ratio")
    right = _panel(canvas, (765, 125, 690, 710), "View lightness difference")
    _histogram(canvas, left, [float(bean["projected_area_ratio_camr_to_caml_median"]) for bean in beans], (92, 168, 105), "area ratio")
    _histogram(canvas, right, [float(bean["lab_l_view_delta_median"]) for bean in beans], (184, 105, 152), "CamR L* - CamL L*")
    cv2.imwrite(str(path), canvas)


def _bean_colour(bean: Mapping[str, Any]) -> tuple[int, int, int]:
    b = int(np.clip((float(bean["caml_mean_b_median"]) + float(bean["camr_mean_b_median"])) * 0.5, 0, 255))
    g = int(np.clip((float(bean["caml_mean_g_median"]) + float(bean["camr_mean_g_median"])) * 0.5, 0, 255))
    r = int(np.clip((float(bean["caml_mean_r_median"]) + float(bean["camr_mean_r_median"])) * 0.5, 0, 255))
    return b, g, r


def _warm_feature_kernel() -> None:
    """Move one-time OpenCV colour-table setup outside recorded timings."""

    image = np.zeros((64, 64, 3), dtype=np.uint8)
    mask = np.zeros((64, 64), dtype=np.uint8)
    cv2.ellipse(mask, (32, 32), (12, 8), 0, 0, 360, 255, -1)
    image[mask > 0] = (60, 110, 180)
    extract_view_features(image, mask, area_scale_mm2_per_px=0.01)


def _representative_jpeg(left: np.ndarray, right: np.ndarray, left_mask: np.ndarray, right_mask: np.ndarray) -> bytes:
    def rendered(image, mask):
        result = image.copy()
        contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(result, contours, -1, (40, 255, 40), 2, cv2.LINE_AA)
        return cv2.resize(result, (180, 180), interpolation=cv2.INTER_AREA)

    paired = np.hstack((rendered(left, left_mask), rendered(right, right_mask)))
    ok, encoded = cv2.imencode(".jpg", paired, (cv2.IMWRITE_JPEG_QUALITY, 91))
    if not ok:
        raise RuntimeError("could not encode representative crop")
    return encoded.tobytes()


def _write_outliers(path: Path, beans: Sequence[Mapping[str, Any]], representatives: Mapping[BeanRef, bytes], groups: Mapping[BeanRef, Sequence[Mapping[str, Any]]]) -> None:
    refs_by_id = {str(ref): ref for ref in groups}
    ranked = sorted(beans, key=lambda bean: float(bean["appearance_outlier_score"]), reverse=True)[:24]
    tiles = []
    for rank, bean in enumerate(ranked, 1):
        ref = refs_by_id.get(str(bean["bean_id"]))
        if ref is None or ref not in representatives:
            continue
        image = cv2.imdecode(np.frombuffer(representatives[ref], np.uint8), cv2.IMREAD_COLOR)
        label = f"#{rank} {bean['bean_id']} score={float(bean['appearance_outlier_score']):.2f} {bean['appearance_flags']}"
        tile = np.full((230, 380, 3), 244, np.uint8)
        tile[5:185, 10:370] = image
        cv2.putText(tile, label[:52], (10, 207), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (30, 30, 30), 1, cv2.LINE_AA)
        cv2.putText(tile, "CamL                            CamR", (18, 224), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (70, 70, 70), 1, cv2.LINE_AA)
        tiles.append(tile)
        cv2.imwrite(str(path / f"{rank:02d}-bean-{ref.sequence:06d}.png"), image)
    columns = 4
    rows = max(1, math.ceil(len(tiles) / columns))
    sheet = np.full((70 + rows * 230, columns * 380, 3), 250, np.uint8)
    cv2.putText(sheet, "Highest calibrated-colour outlier scores (review candidates, not automatic rejects)", (25, 43), cv2.FONT_HERSHEY_SIMPLEX, 0.78, (25, 25, 25), 2, cv2.LINE_AA)
    for index, tile in enumerate(tiles):
        row, column = divmod(index, columns)
        sheet[70 + row * 230 : 70 + (row + 1) * 230, column * 380 : (column + 1) * 380] = tile
    cv2.imwrite(str(path / "contact-sheet.png"), sheet)


def _file_inventory(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item != root / "manifest.json"
    ):
        result.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    return result


def refresh_bundle_manifest(root: Path) -> None:
    """Refresh a bundle's embedded summary and non-recursive file inventory."""

    root = root.expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["summary"] = _read_json(root / "summary.json")
    manifest["files"] = _file_inventory(root)
    _write_json(manifest_path, manifest)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"cannot read JSON document {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceError(f"JSON document must be an object: {path}")
    return value


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


if __name__ == "__main__":
    main()
