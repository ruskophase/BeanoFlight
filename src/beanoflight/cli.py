"""Command-line entry point for the BeanoFlight desktop application."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .app import BeanoFlightApp
from .sorting_context_transport import DEFAULT_SORTING_CONTEXT_ENDPOINT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beano-flight",
        description="Review CamL bean detections, tracks and sorting-line predictions.",
    )
    parser.add_argument(
        "recording",
        nargs="?",
        type=Path,
        help="CamL-calibrated.mkv, postprocess directory, or FastCap recording directory",
    )
    parser.add_argument(
        "--homography",
        type=Path,
        help="PinkPlane v2 homography (normally found beside the FastCap video)",
    )
    parser.add_argument(
        "--hole-pitch-mm",
        type=float,
        default=9.16,
        help="PinkPlane horizontal/vertical hole-centre pitch (default: 9.16)",
    )
    parser.add_argument(
        "--sorting-offset-mm",
        type=float,
        default=30.0,
        help="virtual sorting line below the physical FoV bottom (default: 30)",
    )
    parser.add_argument(
        "--performance-mode",
        action="store_true",
        help=(
            "start in Simulation mode with RAW mmap, prebuffering, and live "
            "playback disabled"
        ),
    )
    parser.add_argument(
        "--sorting-contexts",
        default=DEFAULT_SORTING_CONTEXT_ENDPOINT,
        help="real-time track/prediction context endpoint for BeanoSorter",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.hole_pitch_mm <= 0:
        parser.error("--hole-pitch-mm must be positive")
    if args.sorting_offset_mm <= 0:
        parser.error("--sorting-offset-mm must be positive")
    app = BeanoFlightApp(
        initial_path=args.recording,
        homography_path=args.homography,
        hole_pitch_mm=args.hole_pitch_mm,
        sorting_offset_mm=args.sorting_offset_mm,
        performance_mode=args.performance_mode,
        sorting_context_endpoint=args.sorting_contexts,
    )
    app.mainloop()


if __name__ == "__main__":
    main()
