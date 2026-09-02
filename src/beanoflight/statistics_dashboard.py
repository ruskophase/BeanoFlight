"""Self-contained browser dashboard for a live Statistics Bundle."""

from __future__ import annotations

import json
import math
from importlib import resources
from pathlib import Path
from typing import Any

DASHBOARD_SCHEMA = "beanoflight-statistics-dashboard/v1"

_FIELDS = (
    "bean_id",
    "sequence",
    "first_frame",
    "sample_count",
    "lightness",
    "lab_a",
    "lab_b",
    "chroma",
    "red",
    "green",
    "blue",
    "caml_red",
    "caml_green",
    "caml_blue",
    "camr_red",
    "camr_green",
    "camr_blue",
    "caml_lightness",
    "caml_lab_a",
    "caml_lab_b",
    "caml_chroma",
    "camr_lightness",
    "camr_lab_a",
    "camr_lab_b",
    "camr_chroma",
    "caml_area",
    "camr_area",
    "projected_area",
    "volume_proxy",
    "ellipsoid_proxy",
    "area_ratio",
    "lightness_delta",
    "outlier_score",
    "outlier_percentile",
    "lightness_z",
    "robust_lightness_z",
    "dark_2sd",
    "dark_robust",
    "measurement_views",
    "sensor_edge",
    "enrichment_fallback",
)

_ASSETS = ("index.html", "chart.html", "dashboard.css", "dashboard.js")


def write_statistics_dashboard(
    output: Path,
    *,
    beans: list[dict[str, Any]],
    summary: dict[str, Any],
    source_fps: float,
) -> dict[str, Any]:
    """Write an offline, file://-safe dashboard and its compact data payload."""

    output.mkdir(parents=True, exist_ok=False)
    package_assets = resources.files("beanoflight").joinpath("dashboard_assets")
    for name in _ASSETS:
        (output / name).write_text(
            package_assets.joinpath(name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    rows = [_dashboard_row(bean) for bean in beans]
    first_frames = [row[2] for row in rows if row[2] is not None]
    payload = {
        "schema": DASHBOARD_SCHEMA,
        "fields": list(_FIELDS),
        "source_fps": _finite_or_none(source_fps),
        "first_frame": min(first_frames) if first_frames else None,
        "summary": _json_safe(summary),
        "beans": rows,
        "image_policy": {
            "images_available": False,
            "description": (
                "This live batch retained numerical measurements only. "
                "Colour swatches are approximate calibrated mean colours; "
                "no bean crop or texture was stored."
            ),
        },
    }
    data = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    (output / "batch-data.js").write_text(
        f"window.BEANO_BATCH_DATA={data};\n", encoding="utf-8"
    )
    return {
        "schema": DASHBOARD_SCHEMA,
        "path": "dashboard/index.html",
        "bean_rows": len(rows),
        "bytes": sum(path.stat().st_size for path in output.iterdir()),
        "images_available": False,
    }


def _dashboard_row(bean: dict[str, Any]) -> list[Any]:
    red = _mean(
        bean,
        "caml_approx_calibrated_mean_r_median",
        "camr_approx_calibrated_mean_r_median",
    )
    green = _mean(
        bean,
        "caml_approx_calibrated_mean_g_median",
        "camr_approx_calibrated_mean_g_median",
    )
    blue = _mean(
        bean,
        "caml_approx_calibrated_mean_b_median",
        "camr_approx_calibrated_mean_b_median",
    )
    views = _maximum_finite(
        bean.get("projected_area_proxy_view_count_median"),
        bean.get("minimum_measurement_view_count"),
    )
    return [
        str(bean.get("bean_id", "")),
        _integer_or_none(bean.get("bean_sequence")),
        _integer_or_none(bean.get("first_frame_index")),
        _integer_or_none(bean.get("sample_count")),
        _number(bean, "combined_approx_lab_l_mean"),
        _number(bean, "combined_approx_lab_a_mean"),
        _number(bean, "combined_approx_lab_b_mean"),
        _number(bean, "combined_approx_lab_chroma_mean"),
        red,
        green,
        blue,
        _number(bean, "caml_approx_calibrated_mean_r_median"),
        _number(bean, "caml_approx_calibrated_mean_g_median"),
        _number(bean, "caml_approx_calibrated_mean_b_median"),
        _number(bean, "camr_approx_calibrated_mean_r_median"),
        _number(bean, "camr_approx_calibrated_mean_g_median"),
        _number(bean, "camr_approx_calibrated_mean_b_median"),
        _number(bean, "caml_approx_lab_l_median"),
        _number(bean, "caml_approx_lab_a_median"),
        _number(bean, "caml_approx_lab_b_median"),
        _number(bean, "caml_approx_lab_chroma_median"),
        _number(bean, "camr_approx_lab_l_median"),
        _number(bean, "camr_approx_lab_a_median"),
        _number(bean, "camr_approx_lab_b_median"),
        _number(bean, "camr_approx_lab_chroma_median"),
        _number(bean, "caml_area_native_px_median"),
        _number(bean, "camr_area_native_px_median"),
        _number(bean, "projected_area_proxy_px_median"),
        _number(bean, "equivalent_sphere_volume_proxy_px3_median"),
        _number(bean, "rotational_ellipsoid_volume_proxy_px3_median"),
        _number(bean, "projected_area_ratio_camr_to_caml_median"),
        _number(bean, "approx_lab_l_view_delta_median"),
        _number(bean, "appearance_outlier_score"),
        _number(bean, "appearance_outlier_percentile"),
        _number(bean, "lightness_z_score"),
        _number(bean, "robust_lightness_z_score"),
        bool(bean.get("dark_candidate_2sd", False)),
        bool(bean.get("dark_candidate_robust", False)),
        _integer_or_none(views),
        bool(bean.get("sensor_edge_observation_count", 0)),
        bool(bean.get("enrichment_fallback_observation_count", 0)),
    ]


def _number(value: dict[str, Any], key: str) -> float | None:
    return _finite_or_none(value.get(key))


def _mean(value: dict[str, Any], left: str, right: str) -> float | None:
    finite = [
        result
        for key in (left, right)
        if (result := _finite_or_none(value.get(key))) is not None
    ]
    return sum(finite) / len(finite) if finite else None


def _maximum_finite(*values: Any) -> float | None:
    finite = [
        result for value in values if (result := _finite_or_none(value)) is not None
    ]
    return max(finite) if finite else None


def _finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer_or_none(value: Any) -> int | None:
    finite = _finite_or_none(value)
    return int(finite) if finite is not None else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
