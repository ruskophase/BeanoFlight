"""Reusable bounded-crop appearance and silhouette measurements."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from .detection import DetectorSettings

_SRGB_LEVELS = np.arange(256, dtype=np.float32) / 255.0
_SRGB_TO_LINEAR = np.where(
    _SRGB_LEVELS <= 0.04045,
    _SRGB_LEVELS / 12.92,
    np.power((_SRGB_LEVELS + 0.055) / 1.055, 2.4),
).astype(np.float32)


@dataclass(frozen=True, slots=True)
class FeatureMeasurement:
    values: dict[str, float]
    mask: np.ndarray
    kernel_ms: float


@dataclass(frozen=True, slots=True)
class PrimitiveMeasurement:
    """Cheap numerical primitives retained for deferred batch processing."""

    values: dict[str, object]
    kernel_ms: float


def extract_view_primitives(
    image_bgr: np.ndarray,
    mask: np.ndarray,
) -> PrimitiveMeasurement:
    """Capture masked colour aggregates and silhouette moments only.

    White balance, colour matrices, perceptual colour spaces, percentiles,
    ellipse fitting and volume proxies intentionally remain offline work.
    """

    started = time.perf_counter_ns()
    if (
        image_bgr.dtype != np.uint8
        or image_bgr.ndim != 3
        or image_bgr.shape[2] != 3
    ):
        raise ValueError("primitive image must be an 8-bit BGR image")
    if mask.dtype != np.uint8 or mask.shape != image_bgr.shape[:2]:
        raise ValueError("primitive mask must match the image")
    binary = np.asarray(mask > 0, dtype=np.uint8) * 255
    mask_count = cv2.countNonZero(binary)
    if mask_count < 25:
        raise ValueError("primitive mask is too small")
    touches_edge = bool(
        np.any(binary[0])
        or np.any(binary[-1])
        or np.any(binary[:, 0])
        or np.any(binary[:, -1])
    )
    moments = cv2.moments(binary, binaryImage=True)
    if moments["m00"] <= 0:
        raise ValueError("primitive mask has no spatial moment")
    centre_x = moments["m10"] / moments["m00"]
    centre_y = moments["m01"] / moments["m00"]
    variance_x = moments["mu20"] / moments["m00"]
    variance_y = moments["mu02"] / moments["m00"]
    covariance_xy = moments["mu11"] / moments["m00"]
    x, y, width, height = cv2.boundingRect(binary)

    colour_mask = cv2.erode(
        binary,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    colour_count = cv2.countNonZero(colour_mask)
    if colour_count < max(25, round(mask_count * 0.55)):
        colour_mask = binary
        colour_count = mask_count
    mean, stddev = cv2.meanStdDev(image_bgr, mask=colour_mask)
    channel_mean = mean.reshape(3)
    channel_std = stddev.reshape(3)
    channel_sum = channel_mean * colour_count
    channel_sum_squares = (
        channel_std * channel_std + channel_mean * channel_mean
    ) * colour_count
    values: dict[str, object] = {
        "mask_area_px": float(mask_count),
        "mask_touches_crop_edge": touches_edge,
        "bbox_x_px": float(x),
        "bbox_y_px": float(y),
        "bbox_width_px": float(width),
        "bbox_height_px": float(height),
        "mask_centroid_x_px": float(centre_x),
        "mask_centroid_y_px": float(centre_y),
        "mask_variance_x_px2": float(variance_x),
        "mask_variance_y_px2": float(variance_y),
        "mask_covariance_xy_px2": float(covariance_xy),
        "colour_pixel_count": float(colour_count),
    }
    for index, name in enumerate(("b", "g", "r")):
        values[f"{name}_sum"] = float(channel_sum[index])
        values[f"{name}_sum_squares"] = float(channel_sum_squares[index])
        values[f"{name}_mean"] = float(channel_mean[index])
        values[f"{name}_std"] = float(channel_std[index])
    return PrimitiveMeasurement(
        values,
        (time.perf_counter_ns() - started) / 1_000_000.0,
    )


def paired_features(
    left: Mapping[str, float],
    right: Mapping[str, float],
    refinement_distance_px: float,
) -> dict[str, float]:
    """Combine two opposing-view measurements into relative size proxies."""

    area = math.sqrt(left["area_mm2"] * right["area_mm2"])
    area_ratio = right["area_mm2"] / max(left["area_mm2"], 1e-12)
    sphere_volume = 4.0 * area**1.5 / (3.0 * math.sqrt(math.pi))
    length = math.sqrt(
        left["ellipse_major_mm"] * right["ellipse_major_mm"]
    )
    width = math.sqrt(
        left["ellipse_minor_mm"] * right["ellipse_minor_mm"]
    )
    ellipsoid_volume = math.pi * length * width**2 / 6.0
    return {
        "projected_area_geomean_mm2": area,
        "projected_area_ratio_camr_to_caml": area_ratio,
        "equivalent_sphere_volume_proxy_mm3": sphere_volume,
        "rotational_ellipsoid_volume_proxy_mm3": ellipsoid_volume,
        "lab_l_view_delta": right["lab_l_mean"] - left["lab_l_mean"],
        "lab_a_view_delta": right["lab_a_mean"] - left["lab_a_mean"],
        "lab_b_view_delta": right["lab_b_mean"] - left["lab_b_mean"],
        "refinement_distance_px": refinement_distance_px,
    }


def foreground_mask(
    current_gray: np.ndarray,
    background_gray: np.ndarray,
    settings: DetectorSettings,
) -> np.ndarray:
    """Reproduce the RAW detector foreground without filtering components."""

    if (
        current_gray.dtype != np.uint8
        or background_gray.dtype != np.uint8
        or current_gray.ndim != 2
        or current_gray.shape != background_gray.shape
    ):
        raise ValueError("foreground inputs must be matching uint8 gray images")
    blur_size = (settings.blur_kernel, settings.blur_kernel)
    difference = cv2.absdiff(
        cv2.GaussianBlur(current_gray, blur_size, 0),
        cv2.GaussianBlur(background_gray, blur_size, 0),
    )
    _unused, result = cv2.threshold(
        difference, settings.threshold, 255, cv2.THRESH_BINARY
    )
    for operation, kernel_size, iterations in (
        (cv2.MORPH_CLOSE, settings.close_kernel, settings.close_iterations),
        (cv2.MORPH_OPEN, settings.open_kernel, settings.open_iterations),
        (cv2.MORPH_DILATE, settings.dilate_kernel, settings.dilate_iterations),
    ):
        if iterations:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
            )
            result = cv2.morphologyEx(
                result, operation, kernel, iterations=iterations
            )
    return result


def component_crop_mask(
    foreground: np.ndarray,
    centroid_native_px: tuple[float, float],
    crop_size_px: int,
    *,
    native_scale: int = 2,
    maximum_distance_px: float = 64.0,
) -> np.ndarray | None:
    """Select the nearest foreground component and align it to a RAW crop."""

    if foreground.dtype != np.uint8 or foreground.ndim != 2:
        raise ValueError("foreground must be a uint8 gray image")
    if crop_size_px <= 0 or native_scale <= 0 or maximum_distance_px <= 0:
        raise ValueError("mask crop geometry must be positive")
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        foreground, connectivity=8
    )
    target = np.asarray(centroid_native_px, dtype=np.float64) / native_scale
    candidates: list[tuple[float, int]] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA]) * native_scale**2
        if area < 100:
            continue
        centre = centroids[label]
        distance = float(np.linalg.norm((centre - target) * native_scale))
        if distance <= maximum_distance_px:
            candidates.append((distance, label))
    if not candidates:
        return None
    _distance, selected = min(candidates, key=lambda item: item[0])
    component = np.asarray(labels == selected, dtype=np.uint8) * 255
    native = cv2.resize(
        component,
        (foreground.shape[1] * native_scale, foreground.shape[0] * native_scale),
        interpolation=cv2.INTER_NEAREST,
    )
    centre_x, centre_y = (round(value) for value in centroid_native_px)
    left = centre_x - crop_size_px // 2
    top = centre_y - crop_size_px // 2
    right = left + crop_size_px
    bottom = top + crop_size_px
    if left < 0 or top < 0 or right > native.shape[1] or bottom > native.shape[0]:
        return None
    result = np.ascontiguousarray(native[top:bottom, left:right])
    if not np.any(result):
        return None
    return result


def extract_view_features(
    image_bgr: np.ndarray,
    mask: np.ndarray,
    *,
    area_scale_mm2_per_px: float | None = None,
) -> FeatureMeasurement:
    """Measure calibrated colour and geometry within one complete silhouette."""

    started = time.perf_counter_ns()
    if (
        image_bgr.dtype != np.uint8
        or image_bgr.ndim != 3
        or image_bgr.shape[2] != 3
    ):
        raise ValueError("feature image must be an 8-bit BGR image")
    if mask.dtype != np.uint8 or mask.shape != image_bgr.shape[:2]:
        raise ValueError("feature mask must match the image")
    binary = np.asarray(mask > 0, dtype=np.uint8) * 255
    if cv2.countNonZero(binary) < 25:
        raise ValueError("feature mask is too small")
    if (
        np.any(binary[0])
        or np.any(binary[-1])
        or np.any(binary[:, 0])
        or np.any(binary[:, -1])
    ):
        raise ValueError("feature mask touches the crop edge")

    contours, _hierarchy = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    contour = max(contours, key=cv2.contourArea)
    area_px = float(cv2.countNonZero(binary))
    contour_area = float(cv2.contourArea(contour))
    perimeter_px = float(cv2.arcLength(contour, True))
    x, y, width, height = cv2.boundingRect(contour)
    hull_area = float(cv2.contourArea(cv2.convexHull(contour)))
    solidity = contour_area / hull_area if hull_area > 0 else 0.0
    extent = area_px / max(float(width * height), 1.0)
    circularity = (
        4.0 * math.pi * contour_area / (perimeter_px**2)
        if perimeter_px > 0
        else 0.0
    )
    equivalent_diameter_px = 2.0 * math.sqrt(area_px / math.pi)
    if len(contour) >= 5:
        _centre, axes, angle = cv2.fitEllipse(contour)
        minor_axis_px = float(min(axes))
        major_axis_px = float(max(axes))
        orientation_deg = float(angle if axes[1] >= axes[0] else (angle + 90.0) % 180.0)
    else:
        minor_axis_px = float(min(width, height))
        major_axis_px = float(max(width, height))
        orientation_deg = math.nan

    silhouette_roi = binary[y : y + height, x : x + width]
    image_roi = image_bgr[y : y + height, x : x + width]
    colour_mask = cv2.erode(
        silhouette_roi,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    if cv2.countNonZero(colour_mask) < max(25, round(area_px * 0.55)):
        colour_mask = silhouette_roi
    selected = colour_mask > 0
    colour_pixels = np.ascontiguousarray(image_roi[selected])
    bgr = colour_pixels.astype(np.float32) / 255.0
    linear_rgb = _SRGB_TO_LINEAR[colour_pixels[:, ::-1]]
    luminance = linear_rgb @ np.asarray((0.2126, 0.7152, 0.0722), np.float32)
    denominator = np.maximum(np.sum(linear_rgb, axis=1), 1e-8)
    chromaticity = linear_rgb / denominator[:, None]

    lab = cv2.cvtColor(
        colour_pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB
    ).reshape(-1, 3).astype(np.float32)
    lab_l = lab[:, 0] * (100.0 / 255.0)
    lab_a = lab[:, 1] - 128.0
    lab_b = lab[:, 2] - 128.0
    chroma = np.hypot(lab_a, lab_b)
    hsv = cv2.cvtColor(
        colour_pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV
    ).reshape(-1, 3).astype(np.float32)
    median_bgr = np.median(colour_pixels, axis=0)
    luminance_p10, luminance_median, luminance_p90 = np.percentile(
        luminance, (10, 50, 90)
    )

    values: dict[str, float] = {
        "area_px": area_px,
        "contour_area_px": contour_area,
        "perimeter_px": perimeter_px,
        "bbox_width_px": float(width),
        "bbox_height_px": float(height),
        "solidity": float(solidity),
        "extent": float(extent),
        "circularity": float(circularity),
        "equivalent_diameter_px": float(equivalent_diameter_px),
        "ellipse_minor_px": minor_axis_px,
        "ellipse_major_px": major_axis_px,
        "ellipse_aspect_ratio": major_axis_px / max(minor_axis_px, 1e-9),
        "ellipse_orientation_deg": orientation_deg,
        "mean_b": float(np.mean(bgr[:, 0]) * 255.0),
        "mean_g": float(np.mean(bgr[:, 1]) * 255.0),
        "mean_r": float(np.mean(bgr[:, 2]) * 255.0),
        "median_b": float(median_bgr[0]),
        "median_g": float(median_bgr[1]),
        "median_r": float(median_bgr[2]),
        "luminance_mean": float(np.mean(luminance)),
        "luminance_p10": float(luminance_p10),
        "luminance_median": float(luminance_median),
        "luminance_p90": float(luminance_p90),
        "luminance_std": float(np.std(luminance)),
        "lab_l_mean": float(np.mean(lab_l)),
        "lab_l_median": float(np.median(lab_l)),
        "lab_a_mean": float(np.mean(lab_a)),
        "lab_b_mean": float(np.mean(lab_b)),
        "lab_chroma_mean": float(np.mean(chroma)),
        "hsv_hue_mean_deg": float(np.mean(hsv[:, 0]) * 2.0),
        "hsv_saturation_mean": float(np.mean(hsv[:, 1]) / 255.0),
        "linear_red_chromaticity": float(np.mean(chromaticity[:, 0])),
        "linear_green_chromaticity": float(np.mean(chromaticity[:, 1])),
        "linear_blue_chromaticity": float(np.mean(chromaticity[:, 2])),
        "highlight_fraction": float(np.mean(np.max(bgr, axis=1) >= (250.0 / 255.0))),
        "shadow_fraction": float(np.mean(luminance <= 0.01)),
        "colour_pixel_count": float(len(bgr)),
    }
    if area_scale_mm2_per_px is not None:
        if not math.isfinite(area_scale_mm2_per_px) or area_scale_mm2_per_px <= 0:
            raise ValueError("metric area scale must be finite and positive")
        linear_scale = math.sqrt(area_scale_mm2_per_px)
        values.update(
            {
                "area_mm2": area_px * area_scale_mm2_per_px,
                "equivalent_diameter_mm": equivalent_diameter_px * linear_scale,
                "ellipse_minor_mm": minor_axis_px * linear_scale,
                "ellipse_major_mm": major_axis_px * linear_scale,
            }
        )
    return FeatureMeasurement(
        values,
        binary,
        (time.perf_counter_ns() - started) / 1_000_000.0,
    )


def local_area_scale(
    point_px: tuple[float, float],
    point_to_mm: Callable[[tuple[float, float]], tuple[float, float]],
    *,
    epsilon_px: float = 1.0,
) -> float:
    """Return the local projected mm² represented by one distorted pixel."""

    if epsilon_px <= 0:
        raise ValueError("area-scale epsilon must be positive")
    x, y = point_px
    centre = np.asarray(point_to_mm((x, y)), dtype=np.float64)
    dx = (np.asarray(point_to_mm((x + epsilon_px, y))) - centre) / epsilon_px
    dy = (np.asarray(point_to_mm((x, y + epsilon_px))) - centre) / epsilon_px
    result = abs(float(np.linalg.det(np.column_stack((dx, dy)))))
    if not math.isfinite(result) or result <= 0:
        raise ValueError("local metric area scale is invalid")
    return result


def numeric_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if math.isfinite(value)], np.float64)
    if finite.size == 0:
        return {"count": 0, "min": None, "p10": None, "p50": None, "p90": None, "p95": None, "max": None, "mean": None}
    return {
        "count": int(finite.size),
        "min": float(np.min(finite)),
        "p10": float(np.percentile(finite, 10)),
        "p50": float(np.percentile(finite, 50)),
        "p90": float(np.percentile(finite, 90)),
        "p95": float(np.percentile(finite, 95)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
    }


def robust_median(rows: Sequence[Mapping[str, float]], key: str) -> float:
    values = [float(row[key]) for row in rows if key in row and math.isfinite(float(row[key]))]
    return float(np.median(values)) if values else math.nan
