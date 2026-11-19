"""Deduplicated, portable full-frame JPEG pack for photo statistics bundles."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .calibration import find_pinkplane_homography
from .source import MMapRawVideoSource, SourceError
from .statistics_frame_viewer import matching_recording, paired_frame_jpegs

FRAME_SCHEMA = "beanoflight-statistics-full-frames/v1"


def _sample_frames(root: Path) -> dict[int, int]:
    photo_index = json.loads((root / "photos/index.json").read_text(encoding="utf-8"))
    result: dict[int, int] = {}
    for bean in photo_index["photos"]:
        for sample in bean.get("samples") or [bean]:
            left = int(sample["frame_index"])
            right = int(sample["right_frame_index"])
            if left in result and result[left] != right:
                raise ValueError(f"conflicting CamR pair for CamL frame {left}")
            result[left] = right
    return result


def write_frame_pack(root: Path, source: MMapRawVideoSource, *, progress=None) -> dict[str, Any]:
    """Write each sampled stereo source frame once; preserve existing packs."""

    root = root.resolve()
    target = root / "frames"
    if target.exists():
        raise FileExistsError(f"full-frame pack already exists: {target}")
    pairs = _sample_frames(root)
    staging = Path(tempfile.mkdtemp(prefix=".frames-", dir=root))
    index = []
    try:
        for ordinal, (left, expected_right) in enumerate(sorted(pairs.items()), 1):
            caml, camr, actual_right = paired_frame_jpegs(source, left)
            if actual_right != expected_right:
                raise SourceError(f"recording pairing changed at CamL frame {left}")
            left_name = f"CamL-{left:06d}.jpg"
            right_name = f"CamR-{actual_right:06d}.jpg"
            (staging / left_name).write_bytes(caml)
            (staging / right_name).write_bytes(camr)
            index.append({
                "frame_index": left,
                "right_frame_index": actual_right,
                "CamL": f"frames/{left_name}",
                "CamR": f"frames/{right_name}",
            })
            if progress is not None and (ordinal % 500 == 0 or ordinal == len(pairs)):
                progress(ordinal, len(pairs))
        (staging / "index.json").write_text(json.dumps({
            "schema": FRAME_SCHEMA, "frame_pairs": index,
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"frame_pairs": len(index), "camera_images": len(index) * 2, "path": "frames/index.json"}


def embed_full_frames(bundle: Path, *, progress=None) -> dict[str, Any]:
    """Enrich an existing bundle, then refresh dashboard links and hashes."""

    bundle = bundle.expanduser().resolve()
    recording = matching_recording(bundle)
    if recording is None:
        raise SourceError("matching source recording is unavailable")
    homography = find_pinkplane_homography(recording)
    if homography is None:
        raise SourceError("source homography is unavailable")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    backgrounds = tuple(int(value) for value in manifest.get("provenance", {}).get("settings", {}).get("background_frame_indices", (2, 8, 14)))
    source = MMapRawVideoSource(recording, crop_processing="calibrated")
    try:
        source.configure_stereo(homography, backgrounds)
        result = write_frame_pack(bundle, source, progress=progress)
    finally:
        source.close()
    from .statistics_bundle import _bundle_readme
    from .statistics_photo_dashboard import add_photo_dashboard_to_bundle

    summary_path = bundle / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["full_frames"] = result
    summary_path.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (bundle / "README.md").write_text(
        _bundle_readme(recording.name, has_photo_dashboard=True), encoding="utf-8"
    )
    add_photo_dashboard_to_bundle(bundle)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    result = embed_full_frames(
        args.bundle,
        progress=lambda done, total: print(f"{done}/{total} unique stereo frames embedded", flush=True),
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
