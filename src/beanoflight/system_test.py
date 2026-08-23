"""Headless recorded-source runner for repeatable multi-process acceptance tests."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .analysis import AnalysisEngine
from .background import parse_background_frame_indices
from .calibration import MetricPlaneCalibration, find_pinkplane_homography
from .crop import BeanCropSelector, CropSettings
from .detection import (
    BeanDetector,
    DetectorSettings,
    RawGreenDetector,
    temporal_median_background,
)
from .inference_transport import DEFAULT_CROP_ENDPOINT
from .prediction import GateLayout
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient
from .replay import CropDispatcher, ReplayRunner, ReplaySettings
from .sorting_context_transport import DEFAULT_SORTING_CONTEXT_ENDPOINT
from .source import MMapRawVideoSource, SourceError, open_replay_source
from .tracking import TrackerSettings


def _background_indices(value: str) -> tuple[int, ...]:
    try:
        return parse_background_frame_indices(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="beano-system-test",
        description=(
            "Replay a CamL recording through tracking, BeanRegistry and optional "
            "crop inference with a bounded prepared-frame buffer."
        ),
    )
    result.add_argument("recording", type=Path)
    result.add_argument(
        "--background-frames",
        required=True,
        type=_background_indices,
        metavar="I0,I1,I2",
        help="3 human-confirmed empty zero-based frame indices",
    )
    result.add_argument("--homography", type=Path)
    input_mode = result.add_mutually_exclusive_group()
    input_mode.add_argument("--prefer-raw", action="store_true")
    input_mode.add_argument(
        "--optimized-raw",
        action="store_true",
        help=(
            "mmap CamL RG10, detect on its half-resolution green plane, and "
            "colour-process only inference crops"
        ),
    )
    result.add_argument(
        "--target-fps",
        type=float,
        default=60.0,
        help="replay rate; zero runs as fast as decoding and analysis allow",
    )
    result.add_argument("--crop-size", type=int, default=224)
    result.add_argument(
        "--crop-processing",
        choices=("ml-fast", "calibrated"),
        default="ml-fast",
        help=(
            "RAW crop preparation: linear sensor BGR for model training/inference "
            "or the calibrated sRGB reference path"
        ),
    )
    result.add_argument("--crops-per-bean", type=int, default=1)
    result.add_argument(
        "--no-adaptive-edge-resize",
        action="store_true",
        help=(
            "defer inference until a complete crop of the requested size fits, "
            "instead of resizing a smaller complete crop near the frame edge"
        ),
    )
    result.add_argument(
        "--prebuffer-frames",
        type=int,
        default=60,
        help="prepared frames held ahead of replay; zero disables buffering",
    )
    result.add_argument(
        "--maximum-frames",
        type=int,
        default=1000,
        help="maximum frames to replay (1-1000)",
    )
    result.add_argument(
        "--keep-stale-frames",
        action="store_true",
        help="process every recorded frame even when replay has fallen behind",
    )
    result.add_argument(
        "--maximum-frame-age-ms",
        type=float,
        default=30.0,
        help="drop replay frames older than this when stale-frame dropping is enabled",
    )
    result.add_argument(
        "--clock-start-lead-ms",
        type=float,
        default=50.0,
        help="future run-clock barrier lead used to absorb startup persistence",
    )
    result.add_argument(
        "--maximum-clock-offset-ms",
        type=float,
        default=2.0,
        help="maximum permitted frame-zero offset from the shared run clock",
    )
    result.add_argument("--no-crops", action="store_true")
    result.add_argument(
        "--no-emergency-microbatch",
        action="store_true",
        help=(
            "keep every busy source frame as one inference batch instead of "
            "advancing a deadline-critical second sample"
        ),
    )
    result.add_argument(
        "--single-view-inference",
        action="store_true",
        help=(
            "transport CamL only and duplicate it at the TensorRT adapter; "
            "intended only for paired-pipeline A/B comparisons"
        ),
    )
    result.add_argument("--registry", default=DEFAULT_COMMAND_ENDPOINT)
    result.add_argument("--crops", default=DEFAULT_CROP_ENDPOINT)
    result.add_argument(
        "--sorting-contexts", default=DEFAULT_SORTING_CONTEXT_ENDPOINT
    )
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
        source = (
            MMapRawVideoSource(
                arguments.recording,
                crop_processing=arguments.crop_processing,
            )
            if arguments.optimized_raw
            else open_replay_source(
                arguments.recording,
                prefer_raw=arguments.prefer_raw,
                cache_frames=3,
            )
        )
        invalid = tuple(
            index
            for index in arguments.background_frames
            if index >= source.metadata.frame_count
        )
        if invalid:
            raise SourceError(f"background frames outside recording: {invalid}")
        if arguments.optimized_raw:
            background = source.build_background(arguments.background_frames)
            detector = RawGreenDetector(DetectorSettings())
            deferred_crop_extractor = source.prepare_crop
        else:
            background = temporal_median_background(
                source.frame(index) for index in arguments.background_frames
            )
            detector = BeanDetector(DetectorSettings())
            deferred_crop_extractor = None
        calibration_path = arguments.homography or find_pinkplane_homography(
            source.path
        )
        if calibration_path is None:
            raise SourceError("could not locate a PinkPlane homography")
        stereo_crop_extractor = None
        if (
            arguments.optimized_raw
            and not arguments.no_crops
            and not arguments.single_view_inference
        ):
            source.configure_stereo(
                calibration_path,
                arguments.background_frames,
            )
            stereo_crop_extractor = source.prepare_stereo_crop
            deferred_crop_extractor = None
        calibration = MetricPlaneCalibration.from_pinkplane(
            calibration_path,
            image_size_px=(source.metadata.width, source.metadata.height),
            hole_pitch_mm=arguments.hole_pitch_mm,
        )
        registry = ZeroMQRegistryClient(arguments.registry, timeout_ms=2_000)
        registry.ping()

        def positions_mapper(points):
            return calibration.pixels_to_mm(source.undistort_points(points))

        engine = AnalysisEngine(
            calibration,
            detector,
            background,
            tracker_settings=TrackerSettings(),
            gate_layout=GateLayout(
                calibration.sorting_line_y(arguments.sorting_offset_mm)
            ),
            registry=registry,
            positions_mapper=positions_mapper if arguments.optimized_raw else None,
        )
        selector = None
        dispatcher = None
        if not arguments.no_crops:
            selector = BeanCropSelector(
                CropSettings(
                    size_px=arguments.crop_size,
                    max_crops_per_bean=arguments.crops_per_bean,
                    adaptive_edge_resize=not arguments.no_adaptive_edge_resize,
                ),
                deferred_extractor=deferred_crop_extractor,
                stereo_extractor=stereo_crop_extractor,
            )
            dispatcher = CropDispatcher(
                arguments.registry,
                arguments.crops,
                emergency_microbatch_enabled=(
                    not arguments.no_emergency_microbatch
                ),
            )
        runner = ReplayRunner(
            source,
            engine,
            registry,
            settings=ReplaySettings(
                target_fps=arguments.target_fps,
                preview_enabled=False,
                prebuffer_frames=arguments.prebuffer_frames,
                maximum_frames=arguments.maximum_frames,
                drop_stale_frames=not arguments.keep_stale_frames,
                maximum_frame_age_ms=arguments.maximum_frame_age_ms,
                clock_start_lead_ms=arguments.clock_start_lead_ms,
                maximum_clock_offset_ms=arguments.maximum_clock_offset_ms,
                emergency_microbatch_enabled=(
                    not arguments.no_emergency_microbatch
                ),
            ),
            crop_selector=selector,
            crop_dispatcher=dispatcher,
            sorting_context_endpoint=arguments.sorting_contexts,
            profile_metadata={
                "name": "headless-system-test",
                "optimized_raw": arguments.optimized_raw,
                "crops_enabled": not arguments.no_crops,
                "stereo_crops": stereo_crop_extractor is not None,
                "adaptive_edge_resize": not arguments.no_adaptive_edge_resize,
                "emergency_microbatch": (
                    not arguments.no_emergency_microbatch
                ),
                "crop_processing": (
                    arguments.crop_processing
                    if arguments.optimized_raw
                    else "calibrated-video"
                ),
                "background": {
                    "method": "explicit human-confirmed frames",
                    "frame_indices": list(arguments.background_frames),
                },
            },
        )

        def progress(value) -> None:
            completed = value.frame_index + 1
            if completed == 1 or completed % arguments.progress_every == 0:
                print(
                    f"frame {completed}/{value.frame_count}: "
                    f"{value.achieved_fps:.1f} FPS, "
                    f"timeline {value.source_timeline_fps:.1f} FPS, "
                    f"age {value.frame_age_ms:.1f} ms, "
                    f"skipped {value.frames_skipped}, "
                    f"read {value.source_read_ms:.2f} ms, "
                    f"analyse {value.processing_ms:.2f} ms",
                    flush=True,
                )

        def prebuffer_progress(buffered: int, target: int) -> None:
            print(f"prebuffer {buffered}/{target} decoded frames", flush=True)

        summary = runner.run(
            on_progress=progress,
            on_prebuffer=prebuffer_progress,
        )
        print(json.dumps(asdict(summary), indent=2), flush=True)
    finally:
        if registry is not None:
            registry.close()
        if source is not None:
            source.close()


if __name__ == "__main__":
    main()
