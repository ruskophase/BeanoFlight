#!/usr/bin/env python3
"""Extract real 224x224 CamL bean crops for the timing-only mock model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from beanoflight.analysis import AnalysisEngine
from beanoflight.calibration import MetricPlaneCalibration, find_pinkplane_homography
from beanoflight.crop import BeanCropSelector, CropSettings
from beanoflight.detection import DetectorSettings, RawGreenDetector
from beanoflight.prediction import GateLayout
from beanoflight.source import MMapRawVideoSource
from beanoflight.tracking import TrackerSettings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("recording", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--background-frames", default="43,222,347")
    result.add_argument("--maximum-frames", type=int, default=601)
    result.add_argument("--crops-per-bean", type=int, default=2)
    result.add_argument("--crop-size", type=int, default=224)
    return result


def main() -> int:
    arguments = parser().parse_args()
    background_indices = tuple(
        int(value.strip())
        for value in arguments.background_frames.split(",")
        if value.strip()
    )
    if not background_indices:
        raise SystemExit("at least one background frame is required")
    output = arguments.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = MMapRawVideoSource(arguments.recording, crop_processing="ml-fast")
    try:
        background = source.build_background(background_indices)
        calibration_path = find_pinkplane_homography(source.path)
        if calibration_path is None:
            raise SystemExit("could not locate the PinkPlane homography")
        calibration = MetricPlaneCalibration.from_pinkplane(
            calibration_path,
            image_size_px=(source.metadata.width, source.metadata.height),
            hole_pitch_mm=9.16,
        )

        def positions_mapper(points):
            return calibration.pixels_to_mm(source.undistort_points(points))

        engine = AnalysisEngine(
            calibration,
            RawGreenDetector(DetectorSettings()),
            background,
            tracker_settings=TrackerSettings(),
            gate_layout=GateLayout(calibration.sorting_line_y(30.0)),
            positions_mapper=positions_mapper,
        )
        selector = BeanCropSelector(
            CropSettings(
                size_px=arguments.crop_size,
                max_crops_per_bean=arguments.crops_per_bean,
                adaptive_edge_resize=True,
            ),
            deferred_extractor=source.prepare_crop,
        )
        records = []
        frame_count = min(arguments.maximum_frames, source.metadata.frame_count)
        for index in range(frame_count):
            frame = source.frame(index)
            try:
                analysis = engine.process(frame, index, source.timestamp_ns(index))
                payloads = selector.select(
                    frame,
                    analysis,
                    {track.bean_ref: 1 for track in analysis.tracks},
                )
                materialized = tuple(payload.materialized() for payload in payloads)
                selector.delivery_succeeded(payloads)
                for payload in materialized:
                    bean = payload.job.bean_ref.sequence
                    sample = int(payload.job.job_id.rsplit(":", 1)[1])
                    name = f"bean-{bean:04d}-frame-{index:04d}-sample-{sample}.png"
                    path = output / name
                    if not cv2.imwrite(str(path), payload.image_bgr):
                        raise RuntimeError(f"could not write {path}")
                    records.append(
                        {
                            "path": name,
                            "bean_sequence": bean,
                            "frame_index": index,
                            "sample_index": sample,
                            "resized": payload.job.resized,
                            "source_crop_size_px": payload.job.source_crop_width_px,
                        }
                    )
            finally:
                source.release_frame(frame)
            if index == 0 or (index + 1) % 100 == 0:
                print(f"frame {index + 1}/{frame_count}: {len(records)} crops", flush=True)
        manifest = {
            "schema": "beanoflight-mock-crop-dataset/v1",
            "purpose": "timing and integration only; labels are not bean truth",
            "recording": str(source.path),
            "background_frames": list(background_indices),
            "crop_size_px": arguments.crop_size,
            "records": records,
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(records)} lossless crops to {output}")
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
