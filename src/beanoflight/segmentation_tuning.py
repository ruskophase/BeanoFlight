"""Read-only RAW replay study of neighbouring-bean segmentation settings.

The study writes a separate diagnostic directory. It never changes a recording,
statistics bundle, detector setting, or production result.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .calibration import find_pinkplane_homography
from .detection import DetectorSettings
from .source import MMapRawVideoSource
from .statistics_features import component_crop_mask, foreground_mask

SEEDS = (120, 1197, 1403, 1550)
BACKGROUND_FRAMES = (2, 8, 14)
VARIANTS = {
    "baseline": {},
    "blur3": {"blur_kernel": 3},
    "close3": {"close_kernel": 3},
    "close1": {"close_kernel": 1},
    "no_close": {"close_iterations": 0},
    "blur3_close3": {"blur_kernel": 3, "close_kernel": 3},
    "threshold26": {"threshold": 26},
}


@dataclass(frozen=True)
class ObservationResult:
    bean_sequence: int
    frame_index: int
    camera: str
    cohort: str
    variant: str
    area_px: int | None
    baseline_area_px: int
    original_area_px: int
    area_mm2: float | None
    centroid_distance_px: float | None
    nearest_competitor_distance_px: float | None
    nearby_component_areas_px: tuple[int, ...]
    split_candidate: bool
    ambiguous: bool
    crop_edge: bool


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def select_cohorts(
    beans: list[dict[str, Any]],
    *,
    seeds: tuple[int, ...] = SEEDS,
    suspicious_count: int = 36,
    control_count: int = 36,
) -> dict[int, str]:
    """Deterministically sample extremes and ordinary beans across the batch."""
    by_sequence = {int(bean["bean_sequence"]): bean for bean in beans}
    missing = set(seeds) - set(by_sequence)
    if missing:
        raise ValueError(f"missing seed beans: {sorted(missing)}")
    result = {number: "seed" for number in seeds}
    usable = [bean for bean in beans if int(bean.get("sample_count") or 0) >= 2]
    areas = [float(bean["projected_area_geomean_mm2_median"]) for bean in usable]
    median_area = statistics.median(areas)

    def suspicion(bean: dict[str, Any]) -> float:
        area = float(bean["projected_area_geomean_mm2_median"])
        ratio = float(bean["projected_area_ratio_camr_to_caml_median"])
        spread = float(bean.get("projected_area_geomean_mm2_p90") or area) - float(
            bean.get("projected_area_geomean_mm2_p10") or area
        )
        return max(0.0, area / median_area - 1.0) + abs(math.log(max(ratio, 1e-6))) + spread / median_area

    ranked = sorted(usable, key=lambda bean: (-suspicion(bean), int(bean["bean_sequence"])))
    for bean in ranked:
        if sum(cohort == "suspect" for cohort in result.values()) >= suspicious_count:
            break
        result.setdefault(int(bean["bean_sequence"]), "suspect")

    # Draw controls from the central half of area and stereo-ratio distributions,
    # spread across the timeline rather than accidentally taking one short burst.
    ordinary = sorted(
        (
            bean for bean in usable
            if int(bean["bean_sequence"]) not in result
            and .75 <= float(bean["projected_area_geomean_mm2_median"]) / median_area <= 1.25
            and .85 <= float(bean["projected_area_ratio_camr_to_caml_median"]) <= 1.15
        ),
        key=lambda bean: int(bean["first_frame_index"]),
    )
    for index in range(control_count):
        if not ordinary:
            break
        bean = ordinary[min(len(ordinary) - 1, round((index + .5) * len(ordinary) / control_count))]
        result.setdefault(int(bean["bean_sequence"]), "control")
    return result


def _nearby_components(
    foreground: np.ndarray, centroid_native: tuple[float, float], *, native_scale: int = 2
) -> tuple[list[tuple[float, int, int]], np.ndarray]:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(foreground, connectivity=8)
    target = np.asarray(centroid_native, np.float64) / native_scale
    components = sorted((
        (float(np.linalg.norm((centroids[label] - target) * native_scale)),
         int(stats[label, cv2.CC_STAT_AREA]) * native_scale**2, label)
        for label in range(1, count)
        if stats[label, cv2.CC_STAT_AREA] * native_scale**2 >= 100
    ))
    return components, labels


def _mask_touches_edge(mask: np.ndarray) -> bool:
    return bool(np.any(mask[0]) or np.any(mask[-1]) or np.any(mask[:, 0]) or np.any(mask[:, -1]))


def _metric(
    observation: dict[str, Any], camera: str, variant: str, cohort: str,
    foreground: np.ndarray,
) -> tuple[ObservationResult, np.ndarray | None]:
    prefix = camera.lower()
    centroid = (float(observation[f"{prefix}_centroid_x_px"]), float(observation[f"{prefix}_centroid_y_px"]))
    crop_size = int(observation["source_crop_size_px"])
    mask = component_crop_mask(foreground, centroid, crop_size, maximum_distance_px=24.0)
    components, labels = _nearby_components(foreground, centroid)
    first_distance = components[0][0] if components else None
    second_distance = components[1][0] if len(components) > 1 else None
    area = cv2.countNonZero(mask) if mask is not None else None
    original_area = int(observation[f"{prefix}_area_px"])
    original_mm2 = float(observation[f"{prefix}_area_mm2"])
    nearby = [component for component in components if component[0] <= 100 and component[1] >= original_area * .12]
    split_candidate = (
        len(nearby) >= 2
        and .68 <= sum(component[1] for component in nearby[:2]) / original_area <= 1.32
    )
    result = ObservationResult(
        bean_sequence=int(observation["bean_sequence"]),
        frame_index=int(observation["frame_index"]),
        camera=camera,
        cohort=cohort,
        variant=variant,
        area_px=area,
        baseline_area_px=original_area,
        original_area_px=original_area,
        area_mm2=None if area is None else area * original_mm2 / original_area,
        centroid_distance_px=first_distance,
        nearest_competitor_distance_px=second_distance,
        nearby_component_areas_px=tuple(component[1] for component in nearby[:3]),
        split_candidate=split_candidate,
        ambiguous=bool(first_distance is not None and second_distance is not None and second_distance <= 24 and second_distance - first_distance < 10),
        crop_edge=mask is not None and _mask_touches_edge(mask),
    )
    if mask is None and split_candidate:
        crop_size = int(observation["source_crop_size_px"])
        left = round(centroid[0]) - crop_size // 2
        top = round(centroid[1]) - crop_size // 2
        visual = np.zeros((crop_size, crop_size), np.uint8)
        for index, (_distance, _area, label) in enumerate(nearby[:2], 1):
            component = np.asarray(labels == label, np.uint8)
            native = cv2.resize(component, (labels.shape[1] * 2, labels.shape[0] * 2), interpolation=cv2.INTER_NEAREST)
            visual[native[top : top + crop_size, left : left + crop_size] > 0] = index
        return result, visual
    return result, mask


def _summarize(results: list[ObservationResult]) -> dict[str, Any]:
    by_variant: dict[str, list[ObservationResult]] = defaultdict(list)
    for result in results:
        by_variant[result.variant].append(result)
    baseline = {(row.bean_sequence, row.frame_index, row.camera): row for row in by_variant["baseline"]}
    summary = {}
    for variant, rows in by_variant.items():
        control = [row for row in rows if row.cohort == "control" and row.area_px is not None]
        suspect = [row for row in rows if row.cohort in {"seed", "suspect"}]
        drift = [abs(row.area_px / baseline[(row.bean_sequence, row.frame_index, row.camera)].area_px - 1)
                 for row in control if baseline[(row.bean_sequence, row.frame_index, row.camera)].area_px]
        reductions = [1 - row.area_px / baseline[(row.bean_sequence, row.frame_index, row.camera)].area_px
                      for row in suspect if row.area_px is not None and baseline[(row.bean_sequence, row.frame_index, row.camera)].area_px]
        summary[variant] = {
            "observations": len(rows),
            "missing_masks": sum(row.area_px is None for row in rows),
            "split_candidates": sum(row.split_candidate for row in rows),
            "split_without_target_identity": sum(row.split_candidate and row.area_px is None for row in rows),
            "ambiguous_selections": sum(row.ambiguous for row in rows),
            "crop_edge_masks": sum(row.crop_edge for row in rows),
            "control_median_absolute_area_change": statistics.median(drift) if drift else None,
            "control_p90_absolute_area_change": float(np.percentile(drift, 90)) if drift else None,
            "suspect_median_area_reduction": statistics.median(reductions) if reductions else None,
            "suspect_reduced_over_20_percent": sum(value > .2 for value in reductions),
        }
    matched = [row for row in by_variant["baseline"] if row.area_px == row.original_area_px]
    summary["baseline_reproduction"] = {"exact": len(matched), "total": len(by_variant["baseline"])}
    return summary


def _overlay(photo: Path, mask: np.ndarray | None, label: str) -> np.ndarray:
    image = cv2.imread(str(photo), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read photo {photo}")
    if mask is not None:
        if mask.shape != image.shape[:2]:
            mask = cv2.resize(mask, image.shape[1::-1], interpolation=cv2.INTER_NEAREST)
        for value, colour in ((1, (0, 255, 65)), (2, (0, 190, 255))):
            binary = np.asarray(mask == value, np.uint8) * 255 if mask.max() <= 2 else mask
            contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(image, contours, -1, colour, 2)
            if mask.max() > 2:
                break
    cv2.rectangle(image, (0, 0), (image.shape[1], 24), (15, 30, 20), -1)
    cv2.putText(image, label, (5, 17), cv2.FONT_HERSHEY_SIMPLEX, .46, (255, 255, 255), 1, cv2.LINE_AA)
    return image


def write_study_report(
    output: Path,
    summary: dict[str, Any],
    results: list[ObservationResult],
    *,
    seeds: tuple[int, ...] = SEEDS,
) -> None:
    baseline = summary["baseline_reproduction"]
    lines = [
        "# Neighbouring-bean segmentation study",
        "",
        "This is an offline diagnostic on a **test-only recording**. No statistics, photos,",
        "tracking decisions, or production detector settings were changed.",
        "",
        f"The replay exactly reproduced {baseline['exact']}/{baseline['total']} original CamL/CamR masks.",
        "The cohort contains the four named examples, high-area/stereo/temporal outliers,",
        "and ordinary controls spread across the recording. Each retained observation",
        "was tested in both cameras under seven fixed settings.",
        "",
        "| Variant | Split candidates | Splits with no target identity | Missing masks | Median control area change | P90 control area change |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in VARIANTS:
        item = summary[variant]
        lines.append(
            f"| {variant} | {item['split_candidates']} | {item['split_without_target_identity']} | "
            f"{item['missing_masks']} | {item['control_median_absolute_area_change']:.2%} | "
            f"{item['control_p90_absolute_area_change']:.2%} |"
        )
    lines.extend([
        "",
        "A 'split candidate' means the original large component became two plausible",
        "components of combined comparable area. It does **not** establish which component",
        "belongs to the saved bean track. The original centroid often falls in the gap,",
        "so the existing 24-pixel nearest-component rule returns no mask. These are",
        "unresolved identities, not safe corrected measurements.",
        "",
        "## Named examples",
        "",
        "The green/orange outlines in a panel show two separate components when the",
        "saved track centroid cannot safely choose one. Green alone is the selected mask.",
        "",
        "| Bean | Close3 split views/observations | CamL comparison | CamR comparison |",
        "| ---: | ---: | --- | --- |",
    ])
    for sequence in seeds:
        count = sum(row.bean_sequence == sequence and row.variant == "close3" and row.split_candidate for row in results)
        lines.append(
            f"| #{sequence} | {count} | [CamL](bean-{sequence:06d}-CamL-variants.png) | "
            f"[CamR](bean-{sequence:06d}-CamR-variants.png) |"
        )
    lines.extend([
        "",
        "## Decision",
        "",
        "Do not apply a global setting from this sweep. Closing at 3 separates useful",
        "pairs while barely changing ordinary controls, but assigning a separated",
        "component to the *correct tracked bean* needs additional temporal/stereo evidence.",
        "Close1/no-close cause more unresolved identities; blur/threshold changes alone",
        "do not reliably separate the named examples. Genuinely touching or occluded",
        "beans remain unresolved rather than being force-split.",
        "",
        "Next candidate: use a neighbouring-frame track prediction and cross-view",
        "appearance/position evidence to assign separated components, with an explicit",
        "'uncertain' outcome. Validate on natural close pairs, isolated controls, and",
        "synthetic close pairs with known identities before changing live processing.",
        "The source observations and all per-view outcomes are in `results.jsonl`;",
        "configuration and aggregate metrics are in `summary.json`.",
        "",
    ])
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")


def run_study(recording: Path, bundle: Path, output: Path, *, seeds: tuple[int, ...] = SEEDS) -> dict[str, Any]:
    recording, bundle, output = recording.resolve(), bundle.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"study output already exists: {output}")
    beans = _read_jsonl(bundle / "beans.jsonl")
    observations = _read_jsonl(bundle / "observations.jsonl")
    cohorts = select_cohorts(beans, seeds=seeds)
    selected = [row for row in observations if int(row["bean_sequence"]) in cohorts]
    frames: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        frames[int(row["frame_index"])].append(row)
    homography = find_pinkplane_homography(recording)
    if homography is None:
        raise ValueError("recording has no PinkPlane homography")
    source = MMapRawVideoSource(recording, crop_processing="calibrated")
    results: list[ObservationResult] = []
    examples: dict[tuple[int, str, str], np.ndarray | None] = {}
    representative_frames = {int(bean["bean_sequence"]): int(bean["photo_frame_index"])
                             for bean in beans if int(bean["bean_sequence"]) in seeds}
    try:
        source.configure_stereo(homography, BACKGROUND_FRAMES)
        backgrounds = {"CamL": source.build_background(BACKGROUND_FRAMES),
                       "CamR": source.right_background_gray(),
                       "CamR-dual": source.right_background_gray(dual_green=True)}
        for frame_index, frame_rows in sorted(frames.items()):
            frame = source.frame(frame_index)
            try:
                domains = {"CamL": frame.detection_gray,
                           "CamR": source.right_detection_gray(frame)}
                if any(row["camr_mask_domain"] == "dual-green-fallback" for row in frame_rows):
                    domains["CamR-dual"] = source.right_detection_gray(frame, dual_green=True)
                for variant, changes in VARIANTS.items():
                    settings = DetectorSettings().updated(**changes)
                    masks = {domain: foreground_mask(gray, backgrounds[domain], settings)
                             for domain, gray in domains.items()}
                    for row in frame_rows:
                        for camera in ("CamL", "CamR"):
                            domain = "CamR-dual" if camera == "CamR" and row["camr_mask_domain"] == "dual-green-fallback" else camera
                            result, mask = _metric(row, camera, variant, cohorts[int(row["bean_sequence"])], masks[domain])
                            results.append(result)
                            if int(row["bean_sequence"]) in seeds and frame_index == representative_frames[int(row["bean_sequence"])]:
                                examples[(int(row["bean_sequence"]), camera, variant)] = mask
            finally:
                source.release_frame(frame)
    finally:
        source.close()
    summary = _summarize(results)
    if summary["baseline_reproduction"]["exact"] != summary["baseline_reproduction"]["total"]:
        raise ValueError(f"baseline masks failed to reproduce original observations: {summary['baseline_reproduction']}")
    output.mkdir(parents=True)
    (output / "results.jsonl").write_text("".join(json.dumps(asdict(row), sort_keys=True) + "\n" for row in results), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps({"recording": str(recording), "cohorts": cohorts, "variants": VARIANTS, "summary": summary}, indent=2) + "\n", encoding="utf-8")
    for sequence in seeds:
        for camera in ("CamL", "CamR"):
            photo = bundle / "photos" / f"bean-{sequence:06d}-{camera}.jpg"
            panels = [_overlay(photo, examples.get((sequence, camera, variant)), variant) for variant in VARIANTS]
            montage = np.hstack(panels)
            cv2.imwrite(str(output / f"bean-{sequence:06d}-{camera}-variants.png"), montage)
    write_study_report(output, summary, results, seeds=seeds)
    return {"output": str(output), "beans": len(cohorts), "observations": len(selected), "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    recording = args.recording.expanduser().resolve()
    bundle = args.bundle or recording / "postprocess/statistics-bundle"
    output = args.output or recording / "postprocess/segmentation-study-v2"
    print(json.dumps(run_study(recording, bundle, output), indent=2))


if __name__ == "__main__":
    main()
