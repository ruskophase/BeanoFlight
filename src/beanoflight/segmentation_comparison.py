"""Compare independent baseline and experimental offline statistics replays."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2

from .calibration import find_pinkplane_homography
from .detection import DetectorSettings
from .segmentation_tuning import SEEDS
from .source import MMapRawVideoSource
from .statistics_features import component_crop_mask, foreground_mask


def _json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"expected object: {path}")
    return result


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _metrics(beans: list[dict[str, Any]], observations: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    counts = summary.get("counts") or {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        groups[str(row["bean_id"])].append(row)
    area = [value for bean in beans if (value := _finite(bean.get("projected_area_geomean_mm2_median"))) is not None]
    ratio = [value for bean in beans if (value := _finite(bean.get("projected_area_ratio_camr_to_caml_median"))) is not None]
    temporal_spread = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        values = [value for row in rows if (value := _finite(row.get("projected_area_geomean_mm2"))) is not None]
        if len(values) >= 2 and min(values) > 0:
            temporal_spread.append(max(values) / min(values))
    large = sum(value > 70 for value in area)
    stereo_disagree = sum(value < .75 or value > 1.33 for value in ratio)
    inconsistent = sum(value > 1.5 for value in temporal_spread)
    return {
        "confirmed_beans": len(beans),
        "confirmed_tracks": counts.get("confirmed_tracks"),
        "public_tracks": counts.get("public_tracks"),
        "beans_without_valid_stereo_sample": counts.get("beans_without_valid_stereo_sample"),
        "stereo_observations": len(observations),
        "beans_with_one_sample": sum(int(bean.get("sample_count") or 0) == 1 for bean in beans),
        "beans_with_three_samples": sum(int(bean.get("sample_count") or 0) == 3 for bean in beans),
        "area_median_mm2": statistics.median(area),
        "area_over_70_mm2": large,
        "area_over_70_fraction": large / len(area) if area else None,
        "area_over_85_mm2": sum(value > 85 for value in area),
        "area_under_15_mm2": sum(value < 15 for value in area),
        "stereo_ratio_outside_0_75_to_1_33": stereo_disagree,
        "stereo_disagreement_fraction": stereo_disagree / len(ratio) if ratio else None,
        "temporal_area_ratio_over_1_5": inconsistent,
        "temporal_inconsistency_fraction": inconsistent / len(temporal_spread) if temporal_spread else None,
        "temporal_area_ratio_evaluated": len(temporal_spread),
        "sampling_failures": summary.get("sampling_failures", {}),
    }


def _seed_events(
    baseline_beans: list[dict[str, Any]],
    baseline_observations: list[dict[str, Any]],
    experimental_beans: list[dict[str, Any]],
    experimental_observations: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    baseline_by_sequence = {int(bean["bean_sequence"]): bean for bean in baseline_beans}
    experimental_by_id = {str(bean["bean_id"]): bean for bean in experimental_beans}
    result = {}
    for sequence in SEEDS:
        bean = baseline_by_sequence[sequence]
        frame_index = int(bean["photo_frame_index"])
        references = [row for row in baseline_observations if row["bean_id"] == bean["bean_id"]]
        reference = min(references, key=lambda row: abs(int(row["frame_index"]) - frame_index))
        x, y = float(reference["caml_centroid_x_px"]), float(reference["caml_centroid_y_px"])
        candidates = []
        for row in experimental_observations:
            dt = abs(int(row["frame_index"]) - frame_index)
            if dt > 2:
                continue
            dx = abs(float(row["caml_centroid_x_px"]) - x)
            dy = abs(float(row["caml_centroid_y_px"]) - y)
            distance = math.hypot(dx, dy)
            # A falling bean can move >200 native pixels between consecutive
            # 60-Hz frames; compare x tightly, but allow that vertical motion.
            if dx <= 130 and dy <= 130 + 230 * dt:
                candidate = experimental_by_id[str(row["bean_id"])]
                candidates.append({
                    "bean_id": row["bean_id"],
                    "bean_sequence": int(row["bean_sequence"]),
                    "frame_index": int(row["frame_index"]),
                    "frame_delta": dt,
                    "centroid_distance_px": round(distance, 2),
                    "caml_area_mm2": row["caml_area_mm2"],
                    "camr_area_mm2": row["camr_area_mm2"],
                    "bean_median_area_mm2": candidate["projected_area_geomean_mm2_median"],
                    "photo_CamL": candidate.get("photo_CamL"),
                    "photo_CamR": candidate.get("photo_CamR"),
                })
        # Keep one closest observation per alternate track. Multiple tracks in
        # this list are an explicit one-to-many relationship, not a forced match.
        best: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            key = str(candidate["bean_id"])
            current = best.get(key)
            if current is None or (abs(candidate["frame_index"] - frame_index), candidate["centroid_distance_px"]) < (
                abs(current["frame_index"] - frame_index), current["centroid_distance_px"]
            ):
                best[key] = candidate
        result[sequence] = sorted(best.values(), key=lambda item: (item["centroid_distance_px"], item["bean_sequence"]))
    return result


def export_event_overlays(
    recording: Path,
    bundle: Path,
    output: Path,
    events: dict[int, list[dict[str, Any]]],
    beans: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[int, list[str]]:
    """Reproduce and outline exact measured masks on alternate bean photos."""
    selected_ids = {str(item["bean_id"]) for items in events.values() for item in items}
    by_id = {str(bean["bean_id"]): bean for bean in beans if str(bean["bean_id"]) in selected_ids}
    by_id_frame = {(str(row["bean_id"]), int(row["frame_index"])): row for row in observations if str(row["bean_id"]) in selected_ids}
    selected = [(bean, by_id_frame.get((bean_id, int(bean["photo_frame_index"]))))
                for bean_id, bean in by_id.items() if bean.get("photo_frame_index") is not None]
    selected = [(bean, row) for bean, row in selected if row is not None]
    if not selected:
        return {}
    homography = find_pinkplane_homography(recording)
    if homography is None:
        raise ValueError("recording has no PinkPlane homography")
    source = MMapRawVideoSource(recording)
    settings = DetectorSettings().updated(close_kernel=3)
    paths: dict[int, list[str]] = defaultdict(list)
    overlay_dir = output / "mask-overlays"
    overlay_dir.mkdir()
    try:
        source.configure_stereo(homography, (2, 8, 14))
        background = source.build_background((2, 8, 14))
        right_background = source.right_background_gray()
        right_dual_background = source.right_background_gray(dual_green=True)
        for bean, row in selected:
            frame = source.frame(int(row["frame_index"]))
            try:
                left_foreground = foreground_mask(frame.detection_gray, background, settings)
                right_dual = row["camr_mask_domain"] == "dual-green-fallback"
                right_foreground = foreground_mask(
                    source.right_detection_gray(frame, dual_green=right_dual),
                    right_dual_background if right_dual else right_background,
                    settings,
                )
                for camera, foreground in (("CamL", left_foreground), ("CamR", right_foreground)):
                    prefix = camera.lower()
                    photo_path = bundle / str(bean.get(f"photo_{camera}") or "")
                    if not photo_path.is_file():
                        continue
                    image = cv2.imread(str(photo_path), cv2.IMREAD_COLOR)
                    mask = component_crop_mask(
                        foreground,
                        (float(row[f"{prefix}_centroid_x_px"]), float(row[f"{prefix}_centroid_y_px"])),
                        int(row["source_crop_size_px"]),
                        maximum_distance_px=24.0,
                    )
                    if image is None or mask is None or mask.shape != image.shape[:2]:
                        raise ValueError(f"cannot reproduce alternate mask: {bean['bean_id']} {camera}")
                    if cv2.countNonZero(mask) != int(row[f"{prefix}_area_px"]):
                        raise ValueError(f"alternate mask area mismatch: {bean['bean_id']} {camera}")
                    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    cv2.drawContours(image, contours, -1, (0, 255, 65), 2)
                    filename = f"alternate-{int(bean['bean_sequence']):06d}-{camera}.png"
                    cv2.imwrite(str(overlay_dir / filename), image)
                    paths[int(bean["bean_sequence"])].append(f"mask-overlays/{filename}")
            finally:
                source.release_frame(frame)
    finally:
        source.close()
    return paths


def compare(baseline: Path, experimental: Path, output: Path) -> dict[str, Any]:
    baseline, experimental, output = baseline.resolve(), experimental.resolve(), output.resolve()
    baseline_manifest, experimental_manifest = _json(baseline / "manifest.json"), _json(experimental / "manifest.json")
    baseline_recording = baseline_manifest.get("recording")
    if baseline_recording != experimental_manifest.get("recording"):
        raise ValueError("statistics bundles do not refer to the same recording")
    settings = experimental_manifest.get("provenance", {}).get("settings", {})
    if settings.get("detector_close_kernel") != 3:
        raise ValueError("experimental bundle is not the 3x3 closing replay")
    baseline_beans, experimental_beans = _jsonl(baseline / "beans.jsonl"), _jsonl(experimental / "beans.jsonl")
    baseline_obs, experimental_obs = _jsonl(baseline / "observations.jsonl"), _jsonl(experimental / "observations.jsonl")
    metrics = {
        "baseline": _metrics(baseline_beans, baseline_obs, _json(baseline / "summary.json")),
        "close3": _metrics(experimental_beans, experimental_obs, _json(experimental / "summary.json")),
    }
    events = _seed_events(baseline_beans, baseline_obs, experimental_beans, experimental_obs)
    result = {"schema": "beanoflight-segmentation-comparison/v1", "recording": baseline_recording,
              "baseline": str(baseline), "close3": str(experimental), "metrics": metrics, "seed_events": events}
    if output.exists():
        raise FileExistsError(f"comparison output already exists: {output}")
    output.mkdir(parents=True)
    (output / "comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    overlays = export_event_overlays(Path(str(baseline_recording)), experimental, output, events, experimental_beans, experimental_obs)
    lines = ["# Full-recording segmentation replay comparison", "",
             "Both bundles come from the same test-only RAW recording. This comparison",
             "does not establish individual bean identity where an original track splits.", "",
             "| Measure | Original close5 | Experimental close3 |", "| --- | ---: | ---: |"]
    for key in (
        "confirmed_beans", "confirmed_tracks", "public_tracks", "beans_without_valid_stereo_sample",
        "stereo_observations", "beans_with_one_sample", "beans_with_three_samples",
        "area_median_mm2", "area_over_70_mm2", "area_over_70_fraction", "area_over_85_mm2", "area_under_15_mm2",
        "stereo_ratio_outside_0_75_to_1_33", "stereo_disagreement_fraction", "temporal_area_ratio_over_1_5",
        "temporal_inconsistency_fraction",
        "temporal_area_ratio_evaluated",
    ):
        lines.append(f"| {key.replace('_', ' ')} | {metrics['baseline'][key]} | {metrics['close3'][key]} |")
    failure_keys = sorted(set(metrics["baseline"]["sampling_failures"]) | set(metrics["close3"]["sampling_failures"]))
    for key in failure_keys:
        lines.append(
            f"| sampling failure: {key.replace('_', ' ')} | "
            f"{metrics['baseline']['sampling_failures'].get(key, 0)} | "
            f"{metrics['close3']['sampling_failures'].get(key, 0)} |"
        )
    lines.extend(["", "## Named events", "",
                  "Candidate alternate tracks are within two frames and 130 horizontal",
                  "CamL pixels of the original representative observation. Vertical",
                  "tolerance expands by 230 pixels per frame for bean fall. They are nearby tracks,",
                  "not asserted one-to-one matches.", ""])
    for sequence, candidates in events.items():
        lines.extend([f"### Original bean #{sequence}", ""])
        if not candidates:
            lines.append("No nearby alternate sampled track found.")
        else:
            for candidate in candidates:
                links = " · ".join(
                    f"[{path.rsplit('-', 1)[-1].removesuffix('.png')} mask]({path})"
                    for path in overlays.get(candidate["bean_sequence"], [])
                )
                lines.append(
                    f"- Alternate #{candidate['bean_sequence']} at frame {candidate['frame_index']}: "
                    f"{candidate['frame_delta']} frame(s) away, {candidate['centroid_distance_px']} px from original centroid; "
                    f"CamL/CamR area {candidate['caml_area_mm2']:.1f}/{candidate['camr_area_mm2']:.1f} mm²; "
                    f"bean median area {candidate['bean_median_area_mm2']:.1f} mm². {links}"
                )
        lines.append("")
    lines.extend(["A lower count of large or disagreeing beans alone is not proof of",
                  "improvement: changed track counts, sampling coverage and identity",
                  "must be considered together. No live setting is changed by this replay.", ""])
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("experimental", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = compare(args.baseline, args.experimental, args.output)
    print(json.dumps({"output": str(args.output), "metrics": result["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
