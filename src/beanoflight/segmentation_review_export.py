"""Export candidate additional tracks from two offline statistics replays."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


@dataclass(frozen=True)
class Track:
    bean_id: str
    sequence: int
    first_frame: int
    last_frame: int
    photo_frame: int
    sample_count: int
    x: float
    y: float
    observed_frame: int
    velocity_x: float
    velocity_y: float
    photo_caml: str
    photo_camr: str

    def position_at(self, frame: int) -> tuple[float, float]:
        dt = frame - self.observed_frame
        return self.x + self.velocity_x * dt, self.y + self.velocity_y * dt


def _tracks(bundle: Path) -> list[Track]:
    beans = _jsonl(bundle / "beans.jsonl")
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _jsonl(bundle / "observations.jsonl"):
        by_id[str(row["bean_id"])].append(row)
    result = []
    for bean in beans:
        bean_id = str(bean["bean_id"])
        observations = sorted(by_id[bean_id], key=lambda row: int(row["frame_index"]))
        if not observations:
            raise ValueError(f"bean lacks observations: {bean_id}")
        first, last = observations[0], observations[-1]
        span = int(last["frame_index"]) - int(first["frame_index"])
        if span > 0:
            velocity_x = (float(last["caml_centroid_x_px"]) - float(first["caml_centroid_x_px"])) / span
            velocity_y = (float(last["caml_centroid_y_px"]) - float(first["caml_centroid_y_px"])) / span
        else:
            velocity_x, velocity_y = 0.0, 195.0
        result.append(Track(
            bean_id=bean_id,
            sequence=int(bean["bean_sequence"]),
            first_frame=int(bean["first_frame_index"]),
            last_frame=int(bean["last_frame_index"]),
            photo_frame=int(bean["photo_frame_index"]),
            sample_count=int(bean["sample_count"]),
            x=float(first["caml_centroid_x_px"]),
            y=float(first["caml_centroid_y_px"]),
            observed_frame=int(first["frame_index"]),
            velocity_x=max(-40.0, min(40.0, velocity_x)),
            velocity_y=max(80.0, min(320.0, velocity_y)),
            photo_caml=str(bean["photo_CamL"]),
            photo_camr=str(bean["photo_CamR"]),
        ))
    return result


def _match_score(original: Track, candidate: Track) -> float | None:
    if abs(original.first_frame - candidate.first_frame) > 5:
        return None
    if min(original.last_frame, candidate.last_frame) < max(original.first_frame, candidate.first_frame) - 1:
        return None
    frame = max(original.observed_frame, candidate.observed_frame)
    ox, oy = original.position_at(frame)
    cx, cy = candidate.position_at(frame)
    dx, dy = abs(ox - cx), abs(oy - cy)
    if dx > 145 or dy > 155:
        return None
    score = math.hypot(dx, dy) + 10 * abs(original.first_frame - candidate.first_frame)
    return score if score <= 175 else None


def match_tracks(original: list[Track], experimental: list[Track]) -> tuple[dict[str, str], dict[str, list[tuple[str, float]]]]:
    """Deterministic proximity matching; ambiguous splits remain explicit."""
    original_by_frame: dict[int, list[Track]] = defaultdict(list)
    for track in original:
        original_by_frame[track.first_frame].append(track)
    edges: list[tuple[float, str, str]] = []
    candidates: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for trial in experimental:
        for frame in range(trial.first_frame - 5, trial.first_frame + 6):
            for baseline in original_by_frame.get(frame, ()):
                score = _match_score(baseline, trial)
                if score is not None:
                    edges.append((score, baseline.bean_id, trial.bean_id))
                    candidates[trial.bean_id].append((baseline.bean_id, score))
    edges.sort(key=lambda item: (item[0], item[1], item[2]))
    assigned_original: set[str] = set()
    assigned_experimental: set[str] = set()
    matched: dict[str, str] = {}
    for _score, baseline_id, trial_id in edges:
        if baseline_id in assigned_original or trial_id in assigned_experimental:
            continue
        matched[trial_id] = baseline_id
        assigned_original.add(baseline_id)
        assigned_experimental.add(trial_id)
    for key in candidates:
        candidates[key].sort(key=lambda item: (item[1], item[0]))
    return matched, candidates


def _paired_image(left: Path, right: Path, label: str, output: Path) -> None:
    photos = [cv2.imread(str(path), cv2.IMREAD_COLOR) for path in (left, right)]
    if any(image is None for image in photos):
        raise ValueError(f"missing pair photograph: {left} / {right}")
    panels = []
    for camera, image in zip(("CamL", "CamR"), photos, strict=True):
        square = np.full((340, 340, 3), (232, 237, 232), np.uint8)
        height, width = image.shape[:2]
        scale = min(320 / width, 310 / height)
        resized = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        x = (340 - resized.shape[1]) // 2
        y = 28 + (310 - resized.shape[0]) // 2
        square[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
        cv2.putText(square, camera, (12, 19), cv2.FONT_HERSHEY_SIMPLEX, .55, (23, 42, 27), 1, cv2.LINE_AA)
        panels.append(square)
    combined = np.hstack(panels)
    cv2.rectangle(combined, (0, 310), (680, 340), (18, 38, 25), -1)
    cv2.putText(combined, label, (12, 331), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1, cv2.LINE_AA)
    if not cv2.imwrite(str(output), combined):
        raise ValueError(f"cannot write {output}")


def export_review(original_bundle: Path, experimental_bundle: Path, output: Path) -> dict[str, Any]:
    original_bundle, experimental_bundle, output = original_bundle.resolve(), experimental_bundle.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"review output already exists: {output}")
    original, experimental = _tracks(original_bundle), _tracks(experimental_bundle)
    matched, candidates = match_tracks(original, experimental)
    original_by_id = {track.bean_id: track for track in original}
    experimental_by_id = {track.bean_id: track for track in experimental}
    unmatched_experimental = sorted((track for track in experimental if track.bean_id not in matched), key=lambda item: item.sequence)
    unmatched_original = sorted((track for track in original if track.bean_id not in set(matched.values())), key=lambda item: item.sequence)
    if len(unmatched_experimental) - len(unmatched_original) != len(experimental) - len(original):
        raise AssertionError("one-to-one matching inventory does not reconcile")
    photo_index = json.loads((experimental_bundle / "photos/index.json").read_text(encoding="utf-8"))
    photo_by_id = {str(item["bean_id"]): item for item in photo_index["photos"]}
    output.mkdir(parents=True)
    pairs = output / "pairs"
    pairs.mkdir()
    rows = []
    for track in unmatched_experimental:
        near = candidates.get(track.bean_id, [])[:3]
        baseline_ids = [bean_id for bean_id, _score in near]
        sibling_ids = [trial_id for trial_id, baseline_id in matched.items() if baseline_id in baseline_ids]
        metadata = photo_by_id[track.bean_id]
        pair_filename = f"experimental-{track.sequence:06d}-CamL-CamR.jpg"
        _paired_image(
            experimental_bundle / track.photo_caml,
            experimental_bundle / track.photo_camr,
            f"Experimental bean #{track.sequence} · CamL frame {metadata['frame_index']} · CamR frame {metadata['right_frame_index']}",
            pairs / pair_filename,
        )
        rows.append({
            "experimental_bean_id": track.bean_id,
            "experimental_sequence": track.sequence,
            "first_frame": track.first_frame,
            "last_frame": track.last_frame,
            "photo_caml_frame": metadata["frame_index"],
            "photo_camr_frame": metadata["right_frame_index"],
            "sample_count": track.sample_count,
            "pair_image": f"pairs/{pair_filename}",
            "near_original_sequences": [original_by_id[bean_id].sequence for bean_id in baseline_ids],
            "near_original_costs": [round(score, 2) for _bean_id, score in near],
            "matched_experimental_sibling_sequences": [experimental_by_id[bean_id].sequence for bean_id in sibling_ids],
            "interpretation": "candidate split of an existing original track" if sibling_ids else ("no nearby original sampled track" if not near else "near original track, unmatched by one-to-one assignment"),
        })
    result = {
        "schema": "beanoflight-additional-bean-review/v1",
        "original_bundle": str(original_bundle),
        "experimental_bundle": str(experimental_bundle),
        "original_tracks": len(original),
        "experimental_tracks": len(experimental),
        "one_to_one_matches": len(matched),
        "unmatched_experimental": len(unmatched_experimental),
        "unmatched_original": len(unmatched_original),
        "net_additional_tracks": len(experimental) - len(original),
        "matching_rule": "greedy minimum CamL first-observation trajectory distance, within five onset frames and bounded spatial/temporal overlap; candidate classification, not proven bean identity",
        "unmatched_original_sequences": [track.sequence for track in unmatched_original],
        "beans": rows,
    }
    (output / "index.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with (output / "index.csv").open("w", encoding="utf-8", newline="") as stream:
        fieldnames = ("experimental_sequence", "first_frame", "last_frame", "photo_caml_frame", "photo_camr_frame", "sample_count", "pair_image", "near_original_sequences", "matched_experimental_sibling_sequences", "interpretation")
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ";".join(map(str, row[key])) if isinstance(row[key], list) else row[key] for key in fieldnames})
    cards = "\n".join(
        f'<article><img src="{html.escape(row["pair_image"])}" alt="Paired CamL and CamR photographs of experimental bean #{row["experimental_sequence"]}"><h2>Experimental bean #{row["experimental_sequence"]}</h2><p>{html.escape(row["interpretation"])}. Nearby original: {html.escape(str(row["near_original_sequences"]))}; matched sibling: {html.escape(str(row["matched_experimental_sibling_sequences"]))}.</p><p>Frames CamL/CamR {row["photo_caml_frame"]}/{row["photo_camr_frame"]} · {row["sample_count"]} statistical samples</p></article>'
        for row in rows
    )
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Additional bean review</title><style>body{{font:16px system-ui,sans-serif;margin:0;background:#f2f5f1;color:#172a1d}}header{{background:#122b1e;color:white;padding:24px 4vw}}main{{padding:24px 4vw;display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:18px}}article{{background:white;border:1px solid #d4ddd3;border-radius:8px;padding:12px}}img{{width:100%;height:auto}}h2{{font-size:18px}}p{{line-height:1.5}}small{{color:#d8e6d9}}</style></head><body><header><h1>Additional-track bean pairs</h1><p>Net gain: {result["net_additional_tracks"]} · unmatched experimental: {result["unmatched_experimental"]} · unmatched original: {result["unmatched_original"]}</p><small>Offline test recording. These are candidate new tracks from proximity matching, not verified bean identities.</small></header><main>{cards}</main></body></html>'''
    (output / "index.html").write_text(page, encoding="utf-8")
    (output / "README.md").write_text(
        "# Additional-track paired bean review\n\n"
        f"Net track gain: {result['net_additional_tracks']}; unmatched experimental: {result['unmatched_experimental']}; unmatched original: {result['unmatched_original']}.\n\n"
        "Open `index.html` to review paired CamL/CamR images. `index.json` and `index.csv` record exact source frame indices and nearby original tracks. "
        "A one-to-one proximity match is used only to propose which experimental tracks are additional; in a split event either sibling can be the one left unmatched. "
        "This is a test-only recording and the exported candidates require human confirmation.\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_bundle", type=Path)
    parser.add_argument("experimental_bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = export_review(args.original_bundle, args.experimental_bundle, args.output)
    print(json.dumps({key: result[key] for key in ("original_tracks", "experimental_tracks", "one_to_one_matches", "unmatched_experimental", "unmatched_original", "net_additional_tracks")}, indent=2))


if __name__ == "__main__":
    main()
