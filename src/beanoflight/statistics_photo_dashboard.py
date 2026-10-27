"""Full offline statistics dashboard with calibrated paired bean photos."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from .source import SourceError
from .statistics_dashboard import DASHBOARD_SCHEMA, _FIELDS

ASSETS = ("index.html", "chart.html", "dashboard.css", "dashboard.js")


def install_photo_dashboard(
    root: Path,
    *,
    beans: list[dict[str, Any]] | None = None,
    source_fps: float | None = None,
    summary: dict[str, Any] | None = None,
    recording_path: Path | None = None,
) -> dict[str, Any]:
    """Add a paired-photo browser without changing the underlying measurements."""

    root = root.expanduser().resolve()
    destination = root / "dashboard"
    if destination.exists():
        existing = (destination / "batch-data.js")
        if not existing.is_file() or not existing.read_text(encoding="utf-8").startswith(
            ("window.BEANO_PHOTO_DATA=", "window.BEANO_BATCH_DATA=")
        ):
            raise FileExistsError(f"unrecognised dashboard already exists: {destination}")
    if beans is None:
        beans = _read_jsonl(root / "beans.jsonl")
    index = _read_json(root / "photos/index.json")
    if index.get("schema") != "beanoflight-bean-photos/v1":
        raise SourceError("unsupported bean-photo index schema")
    indexed = index.get("photos")
    if not isinstance(indexed, list):
        raise SourceError("bean-photo index must contain a photo list")
    by_id: dict[str, dict[str, Any]] = {}
    for item in indexed:
        if not isinstance(item, dict) or not isinstance(item.get("bean_id"), str):
            raise SourceError("invalid bean-photo index row")
        if item["bean_id"] in by_id:
            raise SourceError(f"duplicate photo bean ID: {item['bean_id']}")
        by_id[item["bean_id"]] = item

    rows: list[dict[str, Any]] = []
    for bean in beans:
        bean_id = str(bean.get("bean_id", ""))
        if not bean_id:
            raise SourceError("statistics bean row has no bean ID")
        pair = by_id.pop(bean_id, None)
        if pair is None:
            if bean.get("photo_CamL") or bean.get("photo_CamR"):
                raise SourceError(
                    f"bean {bean_id} has photos but no matching index row"
                )
            continue
        photos = {}
        for camera in ("CamL", "CamR"):
            relative = pair.get(camera)
            if relative != bean.get(f"photo_{camera}"):
                raise SourceError(
                    f"photo link disagrees with bean row: {bean_id} {camera}"
                )
            photos[camera] = _validated_photo_path(root, relative)
        rows.append({**bean, "photo_CamL": photos["CamL"], "photo_CamR": photos["CamR"]})
    if by_id:
        raise SourceError(
            f"photo index has {len(by_id)} beans absent from statistics rows"
        )
    if not rows:
        raise SourceError("statistics bundle has no paired bean photographs")
    if len({row["bean_id"] for row in rows}) != len(rows):
        raise SourceError("statistics rows contain duplicate bean IDs")
    rows.sort(key=lambda row: row["bean_sequence"])

    if source_fps is None:
        source_fps = _recording_fps(root, recording_path)
    summary = _dashboard_summary(root, rows, summary, recording_path)
    dashboard_rows = [_dashboard_row(bean) for bean in rows]
    lightness = [row[4] for row in dashboard_rows if row[4] is not None]
    if len(lightness) >= 2:
        centre = sum(lightness) / len(lightness)
        deviation = math.sqrt(sum((value - centre) ** 2 for value in lightness) / (len(lightness) - 1))
        for row in dashboard_rows:
            row[35] = (row[4] - centre) / deviation if row[4] is not None and deviation else None
            row[37] = bool(row[35] is not None and row[35] <= -2)
        summary["dark_bean_screen"] = {"candidate_count": sum(row[37] for row in dashboard_rows)}
    first_frames = [row[2] for row in dashboard_rows if row[2] is not None]
    payload = {
        "schema": DASHBOARD_SCHEMA,
        "fields": [*_FIELDS, "photo_CamL", "photo_CamR"],
        "source_fps": _number(source_fps),
        "first_frame": min(first_frames) if first_frames else None,
        "summary": summary,
        "classification": _recording_metadata(root, recording_path).get("classification"),
        "beans": dashboard_rows,
        "image_policy": {"images_available": True, "kind": "calibrated_paired_crops", "geometry_units": "mm"},
    }
    staging = Path(tempfile.mkdtemp(prefix=".photo-dashboard-", dir=root))
    try:
        assets = resources.files("beanoflight").joinpath("dashboard_assets")
        for name in ASSETS:
            content = assets.joinpath(name).read_text(encoding="utf-8")
            if name == "index.html":
                content = _offline_index(content)
            (staging / name).write_text(content, encoding="utf-8")
        (staging / "batch-data.js").write_text(
            "window.BEANO_BATCH_DATA="
            + json.dumps(
                payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            )
            + ";\n",
            encoding="utf-8",
        )
        if destination.exists():
            for name in (*ASSETS, "batch-data.js"):
                (staging / name).replace(destination / name)
        else:
            staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "schema": DASHBOARD_SCHEMA,
        "path": "dashboard/index.html",
        "paired_beans": len(rows),
        "offline_file_safe": True,
        "features": ["appearance", "size_volume", "stereo", "timeline", "review_collection", "paired_photos"],
    }


def add_photo_dashboard_to_bundle(root: Path) -> dict[str, Any]:
    """Upgrade an existing bundle and refresh its hash inventory atomically."""

    from .statistics_bundle import _write_json, refresh_bundle_manifest

    root = root.expanduser().resolve()
    metadata = install_photo_dashboard(root)
    summary_path = root / "summary.json"
    summary = _read_json(summary_path)
    summary["photo_dashboard"] = metadata
    _write_json(summary_path, summary)
    readme = root / "README.md"
    original = readme.read_text(encoding="utf-8")
    description = (
        "Open `dashboard/index.html` for interactive appearance, size/volume, "
        "stereo, timeline and review-collection charts with paired bean photos."
    )
    if "interactive paired-photo browser" in original:
        readme.write_text(
            original.replace(
                "Open `dashboard/index.html` for the interactive paired-photo browser.",
                description,
            ), encoding="utf-8"
        )
    elif "dashboard/index.html" not in original:
        readme.write_text(original.rstrip() + "\n\n" + description + "\n", encoding="utf-8")
    refresh_bundle_manifest(root)
    return metadata


def _dashboard_row(bean: dict[str, Any]) -> list[Any]:
    """Map calibrated offline measurements onto the established chart fields."""
    def n(key: str) -> float | None:
        return _number(bean.get(key))

    def rgb(channel: str) -> float | None:
        values = [n(f"{view}_mean_{channel}_median") for view in ("caml", "camr")]
        valid = [value for value in values if value is not None]
        return sum(valid) / len(valid) if valid else None

    return [
        bean["bean_id"], _integer(bean.get("bean_sequence")),
        _integer(bean.get("first_frame_index")), _integer(bean.get("sample_count")),
        n("combined_lab_l_mean"), n("combined_lab_a_mean"),
        n("combined_lab_b_mean"), n("combined_lab_chroma_mean"),
        rgb("r"), rgb("g"), rgb("b"),
        *[n(f"{view}_mean_{colour}_median") for view in ("caml", "camr") for colour in ("r", "g", "b")],
        *[n(f"{view}_lab_{feature}_mean_median") for view in ("caml", "camr") for feature in ("l", "a", "b", "chroma")],
        n("caml_area_mm2_median"), n("camr_area_mm2_median"),
        n("projected_area_geomean_mm2_median"),
        n("equivalent_sphere_volume_proxy_mm3_median"),
        n("rotational_ellipsoid_volume_proxy_mm3_median"),
        n("projected_area_ratio_camr_to_caml_median"),
        n("lab_l_view_delta_median"), n("appearance_outlier_score"),
        n("appearance_outlier_percentile"), None, None, False, False,
        2, False, False, bean["photo_CamL"], bean["photo_CamR"],
    ]


def _dashboard_summary(
    root: Path, beans: list[dict[str, Any]], source_summary: dict[str, Any] | None = None,
    recording_path: Path | None = None,
) -> dict[str, Any]:
    summary = source_summary if source_summary is not None else _read_json(root / "summary.json")
    result = dict(summary)
    counts = dict(summary.get("counts") or {})
    total = len(beans)
    counts.update(
        confirmed_beans=total,
        beans_with_two_samples=sum(int(bean.get("sample_count") or 0) >= 2 for bean in beans),
        beans_with_one_sample=sum(int(bean.get("sample_count") or 0) == 1 for bean in beans),
        beans_without_samples=sum(int(bean.get("sample_count") or 0) == 0 for bean in beans),
        beans_with_colour=sum(_number(bean.get("combined_lab_l_mean")) is not None for bean in beans),
        derived_observations=counts.get("calibrated_stereo_samples"),
    )
    result["counts"] = counts
    distributions = dict(summary.get("distributions") or {})
    for destination, source in (
        ("projected_area_ratio_camr_to_caml_median", "projected_area_ratio_camr_to_caml_median"),
        ("approx_lab_l_view_delta_median", "lab_l_view_delta_median"),
    ):
        values = sorted(value for bean in beans if (value := _number(bean.get(source))) is not None)
        if values:
            def q(fraction: float) -> float:
                position = fraction * (len(values) - 1)
                lower = int(position)
                upper = min(lower + 1, len(values) - 1)
                return values[lower] + (values[upper] - values[lower]) * (position - lower)
            distributions[destination] = {"p10": q(.1), "p50": q(.5), "p90": q(.9)}
    result["distributions"] = distributions
    recording = _source_recording(root, recording_path)
    result["source_run_id"] = recording.name if recording is not None else root.name
    result["source_capture_statistics"] = {
        "calibrated_stereo_samples": counts.get("calibrated_stereo_samples"),
        "feature_jobs_executed": counts.get("feature_jobs_executed"),
        "frames_processed": counts.get("frames_processed"),
        "extraction_elapsed_seconds": (summary.get("performance") or {}).get("elapsed_seconds"),
    }
    return result


def _offline_index(content: str) -> str:
    replacements = {
        "A relative pixel-domain volume proxy. It is useful for within-setup comparison, not a physical volume measurement.":
            "A calibrated mm³ equivalent-sphere proxy for within-setup size comparison; actual bean volume is not directly measured.",
        "Measurements, not photographs": "Measurements and paired photographs",
        "This live run deliberately retained numerical measurements only. Every colour tile is an approximate calibrated mean of the bean surface seen by CamL and CamR; texture and local defects cannot be reconstructed. Chart selections always map back to exact bean IDs.":
            "This offline test recording includes calibrated CamL/CamR photographs for every bean. Chart selections map to exact bean IDs; select pairs for a review collection. The smaller colour swatches show mean camera colours. This is test evidence, not production validation.",
        "The target is two observations per bean, with an explicit single-sample fallback.":
            "This offline extraction can include multiple calibrated observations per bean; one representative stereo photo pair is retained.",
        "Derived from projected pixel area as 4A³ᐟ²/(3√π). This prioritises a cheap, repeatable relative measure and does not claim physical volume.":
            "Derived from calibrated projected area as 4A³ᐟ²/(3√π), in mm³. It remains a shape-based proxy, not a direct volume measurement.",
        "The geometric mean of both views where available, with one-view fallback. Values are native pixel².":
            "The geometric mean of calibrated projected CamL/CamR areas, in mm².",
        "Opposing cameras see silhouettes, not hidden thickness. These metrics can rank beans under a fixed optical setup, but converting them to cubic millimetres requires a physical calibration and validation objects spanning the useful size range.":
            "Opposing cameras see silhouettes, not hidden thickness. Calibrated mm² area and mm³ equivalent-sphere estimates are useful comparisons, but true physical volume still needs validation against reference objects.",
    }
    for old, new in replacements.items():
        if old not in content:
            raise SourceError(f"dashboard template changed: missing {old[:45]}")
        content = content.replace(old, new)
    return content


def _read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise SourceError(f"expected JSON object at {path}")
    return result


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError) as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise SourceError(f"expected JSON objects in {path}")
    return rows


def _validated_photo_path(root: Path, value: Any) -> str:
    if not isinstance(value, str):
        raise SourceError("missing paired photo path")
    relative = Path(value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "photos"
        or relative.suffix.lower() not in {".jpg", ".jpeg"}
        or not (root / relative).resolve().is_relative_to((root / "photos").resolve())
        or not (root / relative).is_file()
    ):
        raise SourceError(f"invalid or missing paired photo: {value}")
    return "../" + relative.as_posix()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    return int(value) if value is not None else None


def _recording_fps(root: Path, recording_path: Path | None = None) -> float | None:
    return _number(_recording_metadata(root, recording_path).get("plan", {}).get("frame_rate_hz"))


def _source_recording(root: Path, recording_path: Path | None = None) -> Path | None:
    if recording_path is not None:
        recording = recording_path.expanduser().resolve()
        return recording if (recording / "recording.json").is_file() else None
    for candidate in root.parents:
        if (candidate / "recording.json").is_file():
            return candidate
    manifest = root / "manifest.json"
    if manifest.is_file():
        path = _read_json(manifest).get("recording")
        if isinstance(path, str) and (Path(path) / "recording.json").is_file():
            return Path(path).resolve()
    return None


def _recording_metadata(root: Path, recording_path: Path | None = None) -> dict[str, Any]:
    recording = _source_recording(root, recording_path)
    if recording is None:
        return {}
    return _read_json(recording / "recording.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a paired-photo dashboard to an existing Statistics Bundle"
    )
    parser.add_argument("bundle", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(add_photo_dashboard_to_bundle(arguments.bundle), indent=2))


if __name__ == "__main__":
    main()
