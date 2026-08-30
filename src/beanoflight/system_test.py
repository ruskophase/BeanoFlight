"""Headless recorded-source runner for repeatable multi-process acceptance tests."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
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
from .live_statistics import LiveStatisticsCollector, LiveStatisticsSettings
from .prediction import GateLayout
from .registry_service import DEFAULT_COMMAND_ENDPOINT
from .registry_zmq import ZeroMQRegistryClient
from .replay import (
    MAXIMUM_REPLAY_FRAMES,
    CropDispatcher,
    ReplayRunner,
    ReplaySettings,
)
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
    result.add_argument("recording", type=Path, nargs="?")
    result.add_argument(
        "--background-frames",
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
    input_mode.add_argument(
        "--live",
        action="store_true",
        help="consume synchronized cameras directly through headless FastCap",
    )
    result.add_argument(
        "--calibration-pack",
        type=Path,
        help="Camera Tuner final product (default: newest valid bundle)",
    )
    result.add_argument(
        "--state-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="FastCap witness-cache root",
    )
    result.add_argument("--controller-url")
    result.add_argument("--witness-result", type=Path)
    result.add_argument(
        "--live-test-override",
        action="store_true",
        help=(
            "permit a measured failing witness for an explicitly labelled "
            "non-production live workflow test"
        ),
    )
    result.add_argument(
        "--background-samples",
        type=int,
        default=15,
        help="initial synchronized empty pairs used for the live background",
    )
    result.add_argument(
        "--bean-start-delay",
        type=float,
        default=5.0,
        help="seconds between live background completion and pipeline frame zero",
    )
    result.add_argument("--pair-threshold-us", type=float, default=5.0)
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
        help=f"maximum frames to replay (1-{MAXIMUM_REPLAY_FRAMES})",
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
        "--no-statistics",
        action="store_true",
        help="disable the default numerical statistics capture for a live run",
    )
    result.add_argument(
        "--statistics-output-root",
        type=Path,
        help=(
            "statistics capture parent; enables collection for optimized RAW "
            "replay and overrides the live default"
        ),
    )
    result.add_argument("--statistics-crop-size", type=int, default=160)
    result.add_argument("--statistics-queue-capacity", type=int, default=24)
    result.add_argument("--statistics-primary-reserve", type=int, default=8)
    result.add_argument(
        "--statistics-workers",
        type=int,
        choices=(1, 2),
        default=1,
        help="low-priority measurement workers (maximum 2)",
    )
    result.add_argument(
        "--statistics-start-budget-ms",
        type=float,
        default=10.0,
        help=(
            "offer statistics work only when the sorting-critical work already "
            "performed for a frame is within this budget"
        ),
    )
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
    if arguments.no_statistics and arguments.statistics_output_root is not None:
        raise SystemExit(
            "--no-statistics cannot be combined with --statistics-output-root"
        )
    statistics_enabled = (
        (arguments.live and not arguments.no_statistics)
        or arguments.statistics_output_root is not None
    )
    statistics_settings = LiveStatisticsSettings(
        crop_size_px=arguments.statistics_crop_size,
        queue_capacity=arguments.statistics_queue_capacity,
        primary_queue_reserve=arguments.statistics_primary_reserve,
        worker_count=arguments.statistics_workers,
        maximum_preparation_start_ms=arguments.statistics_start_budget_ms,
    )
    try:
        statistics_settings.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if statistics_enabled and not (arguments.live or arguments.optimized_raw):
        raise SystemExit(
            "statistics capture requires --live or --optimized-raw input"
        )
    if arguments.live_test_override and not arguments.live:
        raise SystemExit("--live-test-override requires --live")
    if arguments.live:
        if arguments.recording is not None:
            raise SystemExit("recording must be omitted with --live")
        if arguments.target_fps <= 0:
            raise SystemExit("--live requires a positive --target-fps")
        if arguments.prebuffer_frames:
            raise SystemExit("--live requires --prebuffer-frames 0")
        if arguments.background_frames is not None:
            raise SystemExit("--background-frames is only valid for replay")
        if arguments.background_samples < 3:
            raise SystemExit("--background-samples must be at least 3")
        if arguments.bean_start_delay < 0 or arguments.pair_threshold_us < 0:
            raise SystemExit("live timing values cannot be negative")
    elif arguments.recording is None or arguments.background_frames is None:
        raise SystemExit("replay requires recording and --background-frames")

    source = None
    producer = None
    live_temporary = None
    registry = None
    try:
        if arguments.live:
            from .live_source import (
                FastCapLiveProducer,
                SharedMemoryRawStereoSource,
                resolve_live_calibration_pack,
            )

            calibration_pack = resolve_live_calibration_pack(
                arguments.calibration_pack
            )
            state_root = arguments.state_root.expanduser().resolve()
            state_root.mkdir(parents=True, exist_ok=True)
            live_temporary = tempfile.TemporaryDirectory(
                prefix=".beanoflight-live-", dir=state_root
            )
            live_root = Path(live_temporary.name)
            preview_paths = {
                "CamL": live_root / "CamL.shm",
                "CamR": live_root / "CamR.shm",
            }
            run_seconds = arguments.maximum_frames / arguments.target_fps
            capture_seconds = (
                run_seconds
                + arguments.bean_start_delay
                + arguments.background_samples / arguments.target_fps
                + 10.0
            )

            def fastcap_event(event) -> None:
                name = event.get("event")
                if name in {"controls_configured", "synchronized", "warning"}:
                    print(json.dumps(event), flush=True)

            producer = FastCapLiveProducer(
                calibration_pack,
                state_root,
                preview_paths,
                duration_seconds=capture_seconds,
                controller_url=arguments.controller_url,
                witness_result=arguments.witness_result,
                test_override=arguments.live_test_override,
                event_callback=fastcap_event,
            )
            producer.start()
            source = SharedMemoryRawStereoSource(
                calibration_pack,
                preview_paths,
                frame_count=arguments.maximum_frames,
                fps=arguments.target_fps,
                clock_source_timestamp_ns=producer.clock_source_timestamp_ns,
                clock_monotonic_ns=producer.clock_monotonic_ns,
                pair_threshold_us=arguments.pair_threshold_us,
                crop_processing=arguments.crop_processing,
                producer_check=producer.check_running,
            )
            print(
                f"Acquiring {arguments.background_samples} empty live frame pairs...",
                flush=True,
            )
            background = source.acquire_background(
                arguments.background_samples,
                on_progress=lambda count, total: print(
                    f"live background {count}/{total}", flush=True
                )
                if count == total
                else None,
            )
            print(
                "LIVE_BACKGROUND_READY: begin bean flow now; "
                f"processing starts in {arguments.bean_start_delay:g} seconds",
                flush=True,
            )
            if arguments.bean_start_delay:
                time.sleep(arguments.bean_start_delay)
            detector = RawGreenDetector(DetectorSettings())
            deferred_crop_extractor = source.prepare_crop
            calibration_path = arguments.homography or (
                calibration_pack / "geometry/homography.json"
            )
        else:
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
        statistics_collector = None
        needs_inference_stereo = (
            (arguments.optimized_raw or arguments.live)
            and not arguments.no_crops
            and not arguments.single_view_inference
        )
        if (
            (arguments.optimized_raw or arguments.live)
            and (needs_inference_stereo or statistics_enabled)
        ):
            if not arguments.live:
                source.configure_stereo(
                    calibration_path,
                    arguments.background_frames,
                )
            if needs_inference_stereo:
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
            positions_mapper=(
                positions_mapper
                if arguments.optimized_raw or arguments.live
                else None
            ),
        )
        if statistics_enabled:
            output_root = (
                arguments.statistics_output_root.expanduser().resolve()
                if arguments.statistics_output_root is not None
                else state_root / "live-statistics-captures"
            )
            output_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            statistics_collector = LiveStatisticsCollector(
                source,
                detector,
                background,
                calibration,
                output_root / f"{stamp}-{engine.tracker.run_id[:8]}",
                settings=statistics_settings,
                provenance={
                    "classification": (
                        "test" if arguments.live_test_override else "production"
                    ),
                    "live_test_override": arguments.live_test_override,
                    "source_path": str(source.path),
                    "source_kind": source.source_kind,
                    "calibration_or_recording": str(source.path),
                    "homography": str(calibration_path),
                    "metric_plane": calibration.to_json(),
                    "background": {
                        "method": (
                            "initial synchronized live temporal median"
                            if arguments.live
                            else "explicit human-confirmed frames"
                        ),
                        "frame_indices": (
                            []
                            if arguments.live
                            else list(arguments.background_frames)
                        ),
                        "sample_count": (
                            arguments.background_samples if arguments.live else None
                        ),
                    },
                },
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
            statistics_collector=statistics_collector,
            sorting_context_endpoint=arguments.sorting_contexts,
            profile_metadata={
                "name": "headless-system-test",
                "optimized_raw": arguments.optimized_raw or arguments.live,
                "live_camera_input": arguments.live,
                "classification": (
                    "test" if arguments.live_test_override else "production"
                ),
                "live_test_override": arguments.live_test_override,
                "crops_enabled": not arguments.no_crops,
                "stereo_crops": stereo_crop_extractor is not None,
                "adaptive_edge_resize": not arguments.no_adaptive_edge_resize,
                "emergency_microbatch": (
                    not arguments.no_emergency_microbatch
                ),
                "crop_processing": (
                    arguments.crop_processing
                    if arguments.optimized_raw or arguments.live
                    else "calibrated-video"
                ),
                "statistics_capture": statistics_enabled,
                "background": {
                    "method": (
                        "initial synchronized live temporal median"
                        if arguments.live
                        else "explicit human-confirmed frames"
                    ),
                    "frame_indices": (
                        []
                        if arguments.live
                        else list(arguments.background_frames)
                    ),
                    "sample_count": (
                        arguments.background_samples if arguments.live else None
                    ),
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
        if producer is not None:
            producer.close()
        if live_temporary is not None:
            live_temporary.cleanup()


if __name__ == "__main__":
    main()
