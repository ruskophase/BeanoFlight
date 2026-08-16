"""Headless recorded-source runner for repeatable multi-process acceptance tests."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .analysis import AnalysisEngine
from .calibration import MetricPlaneCalibration, find_pinkplane_homography
from .crop import BeanCropSelector, CropSettings
from .detection import BeanDetector, DetectorSettings, temporal_median_background
from .inference_transport import DEFAULT_CROP_ENDPOINT
from .prediction import GateLayout
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient
from .replay import CropDispatcher, ReplayRunner, ReplaySettings
from .source import SourceError, open_replay_source
from .tracking import TrackerSettings


def _background_indices(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "background frames must be comma-separated integers"
        ) from exc
    if len(result) != 11 or len(set(result)) != 11 or min(result, default=-1) < 0:
        raise argparse.ArgumentTypeError(
            "exactly 11 distinct non-negative background frame indices are required"
        )
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beano-system-test",
        description=(
            "Replay a CamL recording through tracking, BeanRegistry and optional "
            "crop inference without retaining full frames."
        ),
    )
    result.add_argument("recording", type=Path)
    result.add_argument(
        "--background-frames",
        required=True,
        type=_background_indices,
        metavar="I0,I1,...,I10",
        help="11 human-confirmed empty zero-based frame indices",
    )
    result.add_argument("--homography", type=Path)
    result.add_argument("--prefer-raw", action="store_true")
    result.add_argument(
        "--target-fps",
        type=float,
        default=60.0,
        help="replay rate; zero runs as fast as decoding and analysis allow",
    )
    result.add_argument("--crop-size", type=int, default=300)
    result.add_argument("--no-crops", action="store_true")
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    result.add_argument("--crops", default=DEFAULT_CROP_ENDPOINT)
    result.add_argument("--hole-pitch-mm", type=float, default=9.16)
    result.add_argument("--sorting-offset-mm", type=float, default=30.0)
    result.add_argument("--progress-every", type=int, default=60)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    if arguments.target_fps < 0:
        raise SystemExit("--target-fps cannot be negative")
    if arguments.progress_every <= 0:
        raise SystemExit("--progress-every must be positive")
    if arguments.hole_pitch_mm <= 0 or arguments.sorting_offset_mm <= 0:
        raise SystemExit("metric dimensions must be positive")

    source = None
    registry = None
    try:
        source = open_replay_source(
            arguments.recording,
            prefer_raw=arguments.prefer_raw,
            cache_frames=11,
        )
        invalid = tuple(
            index
            for index in arguments.background_frames
            if index >= source.metadata.frame_count
        )
        if invalid:
            raise SourceError(f"background frames outside recording: {invalid}")
        background = temporal_median_background(
            source.frame(index) for index in arguments.background_frames
        )
        calibration_path = arguments.homography or find_pinkplane_homography(
            source.path
        )
        if calibration_path is None:
            raise SourceError("could not locate a PinkPlane homography")
        calibration = MetricPlaneCalibration.from_pinkplane(
            calibration_path,
            image_size_px=(source.metadata.width, source.metadata.height),
            hole_pitch_mm=arguments.hole_pitch_mm,
        )
        registry = ZeroMQRegistryClient(arguments.registry, timeout_ms=2_000)
        registry.ping()
        engine = AnalysisEngine(
            calibration,
            BeanDetector(DetectorSettings()),
            background,
            tracker_settings=TrackerSettings(),
            gate_layout=GateLayout(
                calibration.sorting_line_y(arguments.sorting_offset_mm)
            ),
            registry=registry,
        )
        selector = None
        dispatcher = None
        if not arguments.no_crops:
            selector = BeanCropSelector(CropSettings(size_px=arguments.crop_size))
            dispatcher = CropDispatcher(arguments.registry, arguments.crops)
        runner = ReplayRunner(
            source,
            engine,
            registry,
            settings=ReplaySettings(
                target_fps=arguments.target_fps,
                preview_enabled=False,
            ),
            crop_selector=selector,
            crop_dispatcher=dispatcher,
        )

        def progress(value) -> None:
            completed = value.frame_index + 1
            if completed == 1 or completed % arguments.progress_every == 0:
                print(
                    f"frame {completed}/{value.frame_count}: "
                    f"{value.achieved_fps:.1f} FPS, "
                    f"read {value.source_read_ms:.2f} ms, "
                    f"analyse {value.processing_ms:.2f} ms",
                    flush=True,
                )

        summary = runner.run(on_progress=progress)
        print(json.dumps(asdict(summary), indent=2), flush=True)
    finally:
        if registry is not None:
            registry.close()
        if source is not None:
            source.close()


if __name__ == "__main__":
    main()
