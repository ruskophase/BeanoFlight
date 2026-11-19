"""Known-answer close-neighbour stress test using isolated RAW green-plane beans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .detection import DetectorSettings
from .segmentation_tuning import BACKGROUND_FRAMES, VARIANTS, _read_jsonl
from .source import MMapRawVideoSource
from .statistics_features import foreground_mask

GAPS = (2, 4, 6, 10)


def _isolated_patch(
    gray: np.ndarray, background: np.ndarray, observation: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray] | None:
    foreground = foreground_mask(gray, background, DetectorSettings())
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(foreground, connectivity=8)
    target = np.array([
        float(observation["caml_centroid_x_px"]) / 2,
        float(observation["caml_centroid_y_px"]) / 2,
    ])
    close = sorted(
        (float(np.linalg.norm(centroids[label] - target)), label)
        for label in range(1, count)
        if stats[label, cv2.CC_STAT_AREA] >= 250
    )
    if not close or close[0][0] > 2 or (len(close) > 1 and close[1][0] < 90):
        return None
    label = close[0][1]
    x, y, width, height, area = (int(value) for value in stats[label])
    if not 400 <= area <= 3500 or width > 130 or height > 130:
        return None
    mask = np.asarray(labels[y : y + height, x : x + width] == label, np.uint8)
    return gray[y : y + height, x : x + width].copy(), mask


def _composite(
    background: np.ndarray,
    left: tuple[np.ndarray, np.ndarray],
    right: tuple[np.ndarray, np.ndarray],
    gap: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    image = background.copy()
    left_image, left_mask = left
    right_image, right_mask = right
    height, width = background.shape
    total_width = left_image.shape[1] + right_image.shape[1] + gap
    first_x = (width - total_width) // 2
    second_x = first_x + left_image.shape[1] + gap
    first_y = (height - left_image.shape[0]) // 2
    second_y = (height - right_image.shape[0]) // 2
    truth_left = np.zeros_like(background, np.uint8)
    truth_right = np.zeros_like(background, np.uint8)
    for patch, mask, x, y, truth in (
        (left_image, left_mask, first_x, first_y, truth_left),
        (right_image, right_mask, second_x, second_y, truth_right),
    ):
        tile = image[y : y + patch.shape[0], x : x + patch.shape[1]]
        tile[mask > 0] = patch[mask > 0]
        truth[y : y + patch.shape[0], x : x + patch.shape[1]] = mask
    return image, truth_left, truth_right


def _evaluate(foreground: np.ndarray, left: np.ndarray, right: np.ndarray) -> dict[str, Any]:
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(foreground, connectivity=8)
    def best_label(truth: np.ndarray) -> tuple[int, float]:
        hits = np.bincount(labels[truth > 0], minlength=count)
        if len(hits) <= 1:
            return 0, 0.0
        hits[0] = 0
        label = int(np.argmax(hits))
        intersection = int(hits[label])
        union = int(stats[label, cv2.CC_STAT_AREA]) + cv2.countNonZero(truth) - intersection
        return label, intersection / union if union else 0.0
    left_label, left_iou = best_label(left)
    right_label, right_iou = best_label(right)
    return {
        "separated": bool(left_label and right_label and left_label != right_label),
        "left_iou": left_iou,
        "right_iou": right_iou,
        "accurate": bool(left_label and right_label and left_label != right_label and min(left_iou, right_iou) >= .75),
    }


def run(recording: Path, statistics_bundle: Path, output: Path, *, pair_count: int = 10) -> dict[str, Any]:
    recording, statistics_bundle, output = recording.resolve(), statistics_bundle.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"synthetic study output exists: {output}")
    observations = _read_jsonl(statistics_bundle / "observations.jsonl")
    # Space candidates across the recording; only truly isolated masks survive
    # the direct image check below.
    observations = observations[::max(1, len(observations) // (pair_count * 16))]
    source = MMapRawVideoSource(recording)
    patches: list[tuple[np.ndarray, np.ndarray]] = []
    try:
        background = source.build_background(BACKGROUND_FRAMES)
        for observation in observations:
            frame = source.frame(int(observation["frame_index"]))
            try:
                patch = _isolated_patch(frame.detection_gray, background, observation)
                if patch is not None:
                    patches.append(patch)
            finally:
                source.release_frame(frame)
            if len(patches) >= pair_count * 2:
                break
    finally:
        source.close()
    if len(patches) < 2:
        raise ValueError("not enough isolated RAW beans for synthetic pairs")
    pairs = min(pair_count, len(patches) // 2)
    rows = []
    for pair_index in range(pairs):
        left, right = patches[2 * pair_index : 2 * pair_index + 2]
        for gap in GAPS:
            synthetic, truth_left, truth_right = _composite(background, left, right, gap)
            for variant, changes in VARIANTS.items():
                mask = foreground_mask(synthetic, background, DetectorSettings().updated(**changes))
                rows.append({"pair": pair_index, "gap_px": gap, "variant": variant,
                             **_evaluate(mask, truth_left, truth_right)})
    summary = {}
    for variant in VARIANTS:
        subset = [row for row in rows if row["variant"] == variant]
        summary[variant] = {
            "cases": len(subset),
            "separated": sum(row["separated"] for row in subset),
            "accurate": sum(row["accurate"] for row in subset),
            "by_gap": {str(gap): {
                "separated": sum(row["separated"] for row in subset if row["gap_px"] == gap),
                "accurate": sum(row["accurate"] for row in subset if row["gap_px"] == gap),
            } for gap in GAPS},
        }
    result = {"schema": "beanoflight-segmentation-synthetic/v1", "recording": str(recording),
              "source": "isolated RAW green-plane components pasted onto recorded empty background",
              "pair_count": pairs, "gaps_processing_px": GAPS, "summary": summary, "rows": rows}
    output.mkdir(parents=True)
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = ["# Synthetic close-neighbour validation", "",
             "Known isolated RAW bean silhouettes were pasted onto the recorded empty",
             "background at non-overlapping gaps. This checks separability and mask",
             "overlap against known shapes; it is a ranking test, not proof of natural",
             "track identity or real-occlusion performance.", "",
             "| Variant | Separated | Both masks ≥0.75 IoU | Total |",
             "| --- | ---: | ---: | ---: |"]
    for variant, item in summary.items():
        lines.append(f"| {variant} | {item['separated']} | {item['accurate']} | {item['cases']} |")
    lines.append("")
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pairs", type=int, default=10)
    args = parser.parse_args()
    recording = args.recording.resolve()
    bundle = args.bundle or recording / "postprocess/statistics-bundle"
    output = args.output or recording / "postprocess/segmentation-synthetic-v1"
    result = run(recording, bundle, output, pair_count=args.pairs)
    print(json.dumps({"output": str(output), "pair_count": result["pair_count"], "summary": result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
