"""CamL FFV1 input with exact FastCap timestamp sidecar support."""

from __future__ import annotations

import csv
import json
import math
import mmap
import os
import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import ClassVar, Protocol

import cv2
import numpy as np

from .stereo import (
    StereoCropPreparation,
    StereoPairMetadata,
    StereoPointCalibration,
)

SUPPORTED_VIDEO_EXTENSIONS = {".mkv", ".avi", ".mov", ".mp4", ".m4v", ".webm"}


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    path: Path
    width: int
    height: int
    frame_count: int
    fps: float
    exact_timestamps: bool


class ReplaySource(Protocol):
    metadata: SourceMetadata
    path: Path
    source_kind: str

    def timestamp_ns(self, index: int) -> int: ...

    def frame(self, index: int): ...

    def close(self) -> None: ...


@dataclass(slots=True)
class RawReplayFrame:
    """One mmap-backed Bayer frame plus its compact detection representation."""

    index: int
    path: Path
    detection_gray: np.ndarray
    native_size_px: tuple[int, int]
    _mapping: mmap.mmap | None
    _mosaic: np.ndarray | None
    right_frame_index: int | None = None
    right_timestamp_ns: int | None = None
    right_path: Path | None = None
    _right_mapping: mmap.mmap | None = None
    _right_mosaic: np.ndarray | None = None
    _right_detection_gray: np.ndarray | None = None

    @property
    def mosaic(self) -> np.ndarray:
        if self._mosaic is None:
            raise SourceError("RAW replay frame has already been released")
        return self._mosaic

    @property
    def right_mosaic(self) -> np.ndarray:
        if self._right_mosaic is None:
            raise SourceError("RAW replay frame has no active CamR pair")
        return self._right_mosaic

    def close(self) -> None:
        self._mosaic = None
        mapping = self._mapping
        self._mapping = None
        if mapping is not None:
            mapping.close()
        self._right_detection_gray = None
        self._right_mosaic = None
        right_mapping = self._right_mapping
        self._right_mapping = None
        if right_mapping is not None:
            right_mapping.close()


@dataclass(frozen=True, slots=True)
class _RawStereoPair:
    left_frame_index: int
    right_frame_index: int
    left_timestamp_ns: int
    right_timestamp_ns: int
    left_path: Path
    right_path: Path
    left_offset: int = 0
    right_offset: int = 0


def resolve_caml_video(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        if resolved.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            raise SourceError(f"unsupported video type: {resolved.name}")
        return resolved
    if not resolved.is_dir():
        raise SourceError(f"recording path does not exist: {resolved}")
    candidates = (
        resolved / "postprocess/CamL-calibrated.mkv",
        resolved / "CamL-calibrated.mkv",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SourceError("selected directory contains no CamL-calibrated.mkv")


class RecordingVideoSource:
    """Frame-indexed source that aligns MKV frame n to FastCap pair n."""

    def __init__(self, path: Path, *, cache_frames: int = 6) -> None:
        self.source_kind = "mkv"
        self.path = resolve_caml_video(path)
        self._cache_limit = max(1, cache_frames)
        self._cache: OrderedDict[int, object] = OrderedDict()
        self._capture = self._open()
        self._next_index = 0
        width = round(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = round(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = round(self._capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(self._capture.get(cv2.CAP_PROP_FPS))
        if width <= 0 or height <= 0 or frame_count <= 0:
            self.close()
            raise SourceError("video reports invalid dimensions or frame count")
        if not math.isfinite(fps) or fps <= 0:
            self.close()
            raise SourceError("video reports an invalid frame rate")
        timestamps = _read_pair_timestamps(self.path.parent / "pairs.csv")
        exact = len(timestamps) >= frame_count
        if exact:
            self._timestamps = tuple(timestamps[:frame_count])
        else:
            period_ns = 1_000_000_000.0 / fps
            self._timestamps = tuple(
                round(index * period_ns) for index in range(frame_count)
            )
        self.metadata = SourceMetadata(
            self.path, width, height, frame_count, fps, exact
        )

    def _open(self):
        capture = cv2.VideoCapture(str(self.path), cv2.CAP_FFMPEG)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            capture.release()
            raise SourceError(
                f"OpenCV could not decode {self.path.name}; FFV1 support may be missing"
            )
        return capture

    def timestamp_ns(self, index: int) -> int:
        if not 0 <= index < self.metadata.frame_count:
            raise SourceError(f"frame {index} is outside the video")
        return self._timestamps[index]

    def frame(self, index: int):
        if self._capture is None:
            raise SourceError("video source is closed")
        if not 0 <= index < self.metadata.frame_count:
            raise SourceError(f"frame {index} is outside the video")
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        if self._next_index != index:
            self._seek(index)
        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise SourceError(f"could not decode frame {index + 1}")
        self._next_index = index + 1
        self._cache[index] = frame
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return frame

    def _seek(self, index: int) -> None:
        positioned = bool(self._capture.set(cv2.CAP_PROP_POS_FRAMES, index))
        observed = round(self._capture.get(cv2.CAP_PROP_POS_FRAMES))
        if positioned and observed == index:
            self._next_index = index
            return
        self._capture.release()
        self._capture = self._open()
        self._next_index = 0
        while self._next_index < index:
            if not self._capture.grab():
                raise SourceError(f"could not seek to frame {index + 1}")
            self._next_index += 1

    def clone(self) -> RecordingVideoSource:
        return RecordingVideoSource(self.path, cache_frames=1)

    def close(self) -> None:
        capture = getattr(self, "_capture", None)
        if capture is not None:
            capture.release()
        self._capture = None
        self._cache.clear()

    @staticmethod
    def release_frame(_frame) -> None:
        return

    @staticmethod
    def preview_frame(frame):
        return frame

    def __enter__(self) -> RecordingVideoSource:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class RawBundleVideoSource:
    """Calibrated CamL replay directly from a complete BeanoFastCap bundle."""

    source_kind = "raw-bundle"

    def __init__(
        self,
        bundle: Path,
        *,
        cache_frames: int = 2,
        verify_calibration: bool = True,
    ) -> None:
        root = bundle.expanduser().resolve()
        metadata_path = root / "metadata/CamL.csv"
        calibration_path = root / "calibration"
        if not metadata_path.is_file() or not calibration_path.is_dir():
            raise SourceError(
                "RAW replay requires a complete FastCap bundle with "
                "metadata/CamL.csv and calibration/"
            )
        CalibrationPack, CalibrationProcessor = _fastcap_calibration_types()
        try:
            pack = CalibrationPack.load(calibration_path, verify=verify_calibration)
            self._processor = CalibrationProcessor(pack, "CamL", undistort=True)
            rows = _read_raw_metadata(metadata_path)
            recording = _read_json(root / "recording.json")
            plan = recording.get("plan", {})
            capture = pack.profiles["CamL"]["capture"]
            fps = float(plan.get("frame_rate_hz", 0.0))
            width = int(capture["width"])
            height = int(capture["height"])
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise SourceError(f"cannot open calibrated RAW bundle: {exc}") from exc
        if not rows or fps <= 0:
            raise SourceError("RAW bundle reports no CamL frames or invalid FPS")
        self.path = root
        self._rows = rows
        self._cache_limit = max(1, int(cache_frames))
        self._cache: OrderedDict[int, object] = OrderedDict()
        self.metadata = SourceMetadata(
            root,
            width,
            height,
            len(rows),
            fps,
            True,
        )

    def timestamp_ns(self, index: int) -> int:
        return self._row(index)[0]

    def frame(self, index: int):
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        _timestamp, relative = self._row(index)
        path = (self.path / relative).resolve()
        if self.path not in path.parents:
            raise SourceError(f"RAW frame path escapes recording bundle: {relative}")
        try:
            rgb = self._processor.process_file_srgb8(path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise SourceError(f"cannot decode RAW frame {index + 1}: {exc}") from exc
        frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        self._cache[index] = frame
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return frame

    def close(self) -> None:
        self._cache.clear()

    @staticmethod
    def release_frame(_frame) -> None:
        return

    @staticmethod
    def preview_frame(frame):
        return frame

    def __enter__(self) -> RawBundleVideoSource:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _row(self, index: int) -> tuple[int, Path]:
        if not 0 <= index < len(self._rows):
            raise SourceError(f"frame {index} is outside the RAW bundle")
        return self._rows[index]


class MMapRawVideoSource:
    """Fast CamL replay from mmap-backed RG10 frames.

    Detection uses the two native green samples in every 2x2 Bayer cell.  The
    full mosaic remains mapped only until crop selection for that frame has
    completed, so buffering does not expand RAW frames into full BGR images.
    """

    source_kind = "raw-mmap-green"

    def __init__(self, bundle: Path, *, crop_processing: str = "ml-fast") -> None:
        root = resolve_raw_bundle(bundle)
        metadata_path = root / "metadata/CamL.csv"
        profile_path = root / "calibration/CamL/profile.json"
        try:
            rows = _read_raw_metadata(metadata_path)
            recording = _read_json(root / "recording.json")
            profile = _read_json(profile_path)
            capture = profile["capture"]
            calibration = profile["calibration"]
            width = int(capture["width"])
            height = int(capture["height"])
            stride = int(capture["bytes_per_line"])
            bit_shift = int(capture.get("bit_shift", 0))
            white_level = float(capture["decoded_white_level"])
            dark_level = float(calibration.get("dark_level_median", 0.0))
            fps = float(recording.get("plan", {}).get("frame_rate_hz", 0.0))
            cfa = str(capture["cfa"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceError(f"cannot open memory-mapped RAW bundle: {exc}") from exc
        if not rows or fps <= 0:
            raise SourceError("RAW bundle reports no CamL frames or invalid FPS")
        if width <= 0 or height <= 0 or stride < width * 2 or stride % 2:
            raise SourceError("CamL RAW dimensions or stride are invalid")
        if width % 2 or height % 2:
            raise SourceError("green-plane replay requires even RAW dimensions")
        if cfa != "RGGB":
            raise SourceError(
                f"fast CamL replay currently requires RGGB, received {cfa}"
            )
        if bit_shift < 0 or bit_shift > 15 or white_level <= dark_level:
            raise SourceError("CamL RAW signal levels are invalid")

        self.path = root
        self.profile_path = profile_path
        self.profile = profile
        self._rows = rows
        self._width = width
        self._height = height
        self._stride = stride
        self._bit_shift = bit_shift
        self._expected_bytes = height * stride
        self._active: dict[int, RawReplayFrame] = {}
        self._camera_matrix = np.asarray(calibration["camera_matrix"], dtype=np.float64)
        self._distortion = np.asarray(
            calibration["distortion_coefficients"], dtype=np.float64
        )
        if self._camera_matrix.shape != (3, 3) or self._distortion.size < 4:
            raise SourceError("CamL lens calibration has invalid dimensions")

        levels = np.arange(round(white_level) + 1, dtype=np.float32)
        linear = np.clip(
            (levels - dark_level) / max(white_level - dark_level, 1.0), 0.0, 1.0
        )
        srgb = np.where(
            linear <= 0.0031308,
            linear * 12.92,
            1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
        )
        self._detection_lut = np.clip(srgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
        self._stored_detection_lut = _stored_value_lut(
            self._detection_lut,
            self._bit_shift,
        )
        self._crop_processor = RawCropProcessor(
            profile_path, profile, processing_profile=crop_processing
        )
        self._stereo_calibration: StereoPointCalibration | None = None
        self._stereo_pairs: dict[int, _RawStereoPair] = {}
        self._right_profile: dict[str, object] | None = None
        self._right_rows: tuple[tuple[int, Path, int], ...] = ()
        self._right_width = 0
        self._right_height = 0
        self._right_stride = 0
        self._right_bit_shift = 0
        self._right_expected_bytes = 0
        self._right_detection_lut: np.ndarray | None = None
        self._right_stored_detection_lut: np.ndarray | None = None
        self._right_background: np.ndarray | None = None
        self._right_background_blurred: np.ndarray | None = None
        self._right_fallback_background_blurred: np.ndarray | None = None
        self._right_crop_processor: RawCropProcessor | None = None
        self._stereo_refinement_threshold = 22
        self._stereo_max_refinement_px = 64.0
        self._stereo_search_margin_px = 64
        self._stereo_close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (5, 5)
        )
        self._stereo_open_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (3, 3)
        )
        self._stereo_failure_counts: dict[str, int] = {}
        self._stereo_failure_examples: list[dict[str, object]] = []
        self._stereo_refinement_fallbacks = 0
        self.crop_processing_profile = self._crop_processor.processing_profile
        self.metadata = SourceMetadata(root, width, height, len(rows), fps, True)
        self.pipeline_metadata = {
            "input": "memory-mapped RG10",
            "detection": f"{width // 2}x{height // 2} sRGB green plane",
            "colour": (
                "linear sensor BGR inference crops"
                if self.crop_processing_profile == "ml-fast"
                else "calibrated sRGB inference crops"
            ),
            "crop_processing": self.crop_processing_profile,
            "pixel_coordinate_domain": "distorted RAW",
            "metric_coordinate_domain": "point-undistorted PinkPlane",
            "stereo": "CamL only until configure_stereo()",
        }

    @property
    def stereo_enabled(self) -> bool:
        return self._stereo_calibration is not None

    def stereo_statistics(self) -> dict[str, object]:
        return {
            "enabled": self.stereo_enabled,
            "localizer": "single-green contour boxes with dual-green fallback",
            "dual_green_fallbacks": self._stereo_refinement_fallbacks,
            "failure_counts": dict(sorted(self._stereo_failure_counts.items())),
            "failure_examples": list(self._stereo_failure_examples),
        }

    def _stereo_failure(
        self, reason: str, **detail: object
    ) -> None:
        self._stereo_failure_counts[reason] = (
            self._stereo_failure_counts.get(reason, 0) + 1
        )
        if detail and len(self._stereo_failure_examples) < 8:
            self._stereo_failure_examples.append({"reason": reason, **detail})

    def configure_stereo(
        self,
        homography_path: Path,
        background_indices: tuple[int, ...],
        *,
        refinement_threshold: int = 22,
        maximum_refinement_px: float = 64.0,
        search_margin_px: int = 64,
    ) -> None:
        """Enable synchronized CamR ROI access and local centroid refinement."""

        right_metadata_path = self.path / "metadata/CamR.csv"
        right_profile_path = self.path / "calibration/CamR/profile.json"
        pair_path = self.path / "postprocess/pairs.csv"
        if not pair_path.is_file():
            pair_path = self.path / "pairs.csv"
        try:
            right_rows = _read_raw_metadata(right_metadata_path)
            right_profile = _read_json(right_profile_path)
            capture = right_profile["capture"]
            calibration = right_profile["calibration"]
            right_width = int(capture["width"])
            right_height = int(capture["height"])
            right_stride = int(capture["bytes_per_line"])
            right_bit_shift = int(capture.get("bit_shift", 0))
            right_white_level = float(capture["decoded_white_level"])
            right_dark_level = float(calibration.get("dark_level_median", 0.0))
            right_cfa = str(capture["cfa"])
            pairs = _read_stereo_pairs(pair_path)
            point_calibration = StereoPointCalibration.load(
                homography_path,
                self.profile,
                right_profile,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceError(f"cannot configure synchronized CamR replay: {exc}") from exc
        if not right_rows or not pairs:
            raise SourceError("stereo replay requires CamR metadata and pairs.csv")
        if (
            right_width <= 0
            or right_height <= 0
            or right_width % 2
            or right_height % 2
            or right_stride < right_width * 2
            or right_stride % 2
            or right_cfa != "RGGB"
            or right_bit_shift < 0
            or right_bit_shift > 15
            or right_white_level <= right_dark_level
        ):
            raise SourceError("CamR RAW capture geometry or signal levels are invalid")
        pair_by_left = {pair.left_frame_index: pair for pair in pairs}
        for pair in pairs:
            if not 0 <= pair.left_frame_index < len(self._rows):
                raise SourceError("pairs.csv refers to an invalid CamL frame")
            if not 0 <= pair.right_frame_index < len(right_rows):
                raise SourceError("pairs.csv refers to an invalid CamR frame")
            left_timestamp, left_path, left_offset = self._rows[pair.left_frame_index]
            right_timestamp, right_path, right_offset = right_rows[pair.right_frame_index]
            if (
                left_timestamp != pair.left_timestamp_ns
                or right_timestamp != pair.right_timestamp_ns
                or left_path != pair.left_path
                or right_path != pair.right_path
                or left_offset != pair.left_offset
                or right_offset != pair.right_offset
            ):
                raise SourceError("pairs.csv disagrees with RAW camera metadata")
        if not all(index in pair_by_left for index in range(len(self._rows))):
            raise SourceError("stereo replay requires one CamR pair for every CamL frame")
        if not 0 <= refinement_threshold <= 255:
            raise SourceError("CamR refinement threshold must be between 0 and 255")
        if maximum_refinement_px <= 0 or search_margin_px <= 0:
            raise SourceError("CamR refinement limits must be positive")

        levels = np.arange(round(right_white_level) + 1, dtype=np.float32)
        linear = np.clip(
            (levels - right_dark_level)
            / max(right_white_level - right_dark_level, 1.0),
            0.0,
            1.0,
        )
        srgb = np.where(
            linear <= 0.0031308,
            linear * 12.92,
            1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
        )
        self._right_profile = right_profile
        self._right_rows = right_rows
        self._right_width = right_width
        self._right_height = right_height
        self._right_stride = right_stride
        self._right_bit_shift = right_bit_shift
        self._right_expected_bytes = right_height * right_stride
        self._right_detection_lut = np.clip(
            srgb * 255.0 + 0.5, 0, 255
        ).astype(np.uint8)
        self._right_stored_detection_lut = _stored_value_lut(
            self._right_detection_lut,
            self._right_bit_shift,
        )
        self._right_crop_processor = RawCropProcessor(
            right_profile_path,
            right_profile,
            processing_profile=self.crop_processing_profile,
        )
        self._stereo_pairs = pair_by_left
        self._stereo_calibration = point_calibration
        self._stereo_refinement_threshold = int(refinement_threshold)
        self._stereo_max_refinement_px = float(maximum_refinement_px)
        self._stereo_search_margin_px = int(search_margin_px)
        (
            self._right_background,
            fallback_background,
        ) = self._build_right_background(background_indices)
        self._right_background_blurred = cv2.GaussianBlur(
            self._right_background, (5, 5), 0
        )
        self._right_fallback_background_blurred = cv2.GaussianBlur(
            fallback_background, (5, 5), 0
        )
        self.pipeline_metadata = {
            **self.pipeline_metadata,
            "stereo": "synchronized CamL/CamR RAW ROI pairs",
            "stereo_coordinate_transfer": (
                "CamL distorted -> point undistort -> PinkPlane homography -> "
                "CamR point distort -> local foreground refinement"
            ),
            "stereo_homography": str(point_calibration.homography_path),
            "stereo_background_frames": list(background_indices),
            "stereo_max_refinement_px": self._stereo_max_refinement_px,
            "stereo_localizer": (
                "single Bayer green contour boxes; dual-green fallback"
            ),
        }

    def timestamp_ns(self, index: int) -> int:
        return self._row(index)[0]

    def frame(self, index: int) -> RawReplayFrame:
        _timestamp, relative, raw_offset = self._row(index)
        mapping, mosaic, path = _mmap_raw_mosaic(
            self.path,
            relative,
            raw_offset=raw_offset,
            width=self._width,
            height=self._height,
            stride=self._stride,
            expected_bytes=self._expected_bytes,
            frame_index=index,
            camera_id="CamL",
        )
        try:
            green_stored = cv2.addWeighted(
                mosaic[0::2, 1::2], 0.5, mosaic[1::2, 0::2], 0.5, 0.0
            )
            detection_gray = np.ascontiguousarray(
                self._stored_detection_lut[green_stored]
            )
            pair = self._stereo_pairs.get(index)
            right_mapping = None
            right_mosaic = None
            if pair is not None:
                right_mapping, right_mosaic, right_path = _mmap_raw_mosaic(
                    self.path,
                    pair.right_path,
                    raw_offset=pair.right_offset,
                    width=self._right_width,
                    height=self._right_height,
                    stride=self._right_stride,
                    expected_bytes=self._right_expected_bytes,
                    frame_index=pair.right_frame_index,
                    camera_id="CamR",
                )
            else:
                right_path = None
            frame = RawReplayFrame(
                index=index,
                path=path,
                detection_gray=detection_gray,
                native_size_px=(self._width, self._height),
                _mapping=mapping,
                _mosaic=mosaic,
                right_frame_index=(None if pair is None else pair.right_frame_index),
                right_timestamp_ns=(None if pair is None else pair.right_timestamp_ns),
                right_path=right_path,
                _right_mapping=right_mapping,
                _right_mosaic=right_mosaic,
            )
        except Exception:
            mapping.close()
            right_mapping = locals().get("right_mapping")
            if right_mapping is not None:
                right_mapping.close()
            raise
        self._active[id(frame)] = frame
        return frame

    def build_background(self, indices: tuple[int, ...]) -> np.ndarray:
        if not indices:
            raise SourceError("at least one RAW background frame is required")
        frames: list[np.ndarray] = []
        for index in indices:
            frame = self.frame(index)
            try:
                frames.append(frame.detection_gray.copy())
            finally:
                self.release_frame(frame)
        return np.median(np.stack(frames, axis=0), axis=0).astype(np.uint8)

    def extract_crop(
        self,
        frame: RawReplayFrame,
        centroid_px: tuple[float, float],
        size_px: int,
        *,
        allow_padding: bool,
    ) -> tuple[np.ndarray | None, bool]:
        return self._crop_processor.extract(
            frame.mosaic,
            centroid_px,
            size_px,
            allow_padding=allow_padding,
        )

    def prepare_crop(
        self,
        frame: RawReplayFrame,
        centroid_px: tuple[float, float],
        size_px: int,
        *,
        allow_padding: bool,
    ) -> tuple[Callable[[], np.ndarray], int, int, bool] | None:
        return self._crop_processor.prepare(
            frame.mosaic,
            centroid_px,
            size_px,
            allow_padding=allow_padding,
        )

    def prepare_stereo_crop(
        self,
        frame: RawReplayFrame,
        centroid_px: tuple[float, float],
        size_px: int,
        *,
        allow_padding: bool,
        allow_resize: bool = True,
    ) -> StereoCropPreparation | None:
        calibration = self._stereo_calibration
        right_processor = self._right_crop_processor
        if calibration is None or right_processor is None:
            raise SourceError("synchronized CamR replay has not been configured")
        pair = self._stereo_pairs.get(frame.index)
        if (
            pair is None
            or frame.right_frame_index != pair.right_frame_index
            or frame.right_timestamp_ns != pair.right_timestamp_ns
        ):
            self._stereo_failure("missing_synchronized_pair")
            return None
        projected = calibration.project_distorted_caml_to_distorted_camr(
            centroid_px
        )
        refined = self._refine_right_centroid(frame, projected, size_px)
        if refined is None:
            self._stereo_failure("no_local_camr_component")
            return None
        refined_centroid, area_px, component_size_px = refined
        source_size_px = size_px
        if not allow_padding:
            complete_size = 2 * math.floor(
                min(
                    centroid_px[0],
                    centroid_px[1],
                    self._width - centroid_px[0],
                    self._height - centroid_px[1],
                    refined_centroid[0],
                    refined_centroid[1],
                    self._right_width - refined_centroid[0],
                    self._right_height - refined_centroid[1],
                )
            )
            complete_size -= complete_size % 2
            if complete_size < source_size_px:
                if not allow_resize:
                    self._stereo_failure("camr_crop_clipped_resize_disabled")
                    return None
                # The segmented component already describes the complete bean
                # silhouette. Do not add an arbitrary border here: at the top
                # of the FoV that border can defer an otherwise lossless crop
                # by a full 16.7 ms frame.
                required = math.ceil(max(component_size_px))
                required += required % 2
                if complete_size < max(32, required):
                    self._stereo_failure(
                        "camr_component_or_crop_clipped",
                        left_frame_index=frame.index,
                        caml_centroid_px=list(centroid_px),
                        projected_camr_px=list(projected),
                        refined_camr_px=list(refined_centroid),
                        component_size_px=list(component_size_px),
                        complete_size_px=complete_size,
                        required_size_px=required,
                    )
                    return None
                source_size_px = complete_size
        left = self._crop_processor.prepare(
            frame.mosaic,
            centroid_px,
            source_size_px,
            allow_padding=allow_padding,
        )
        if left is None:
            self._stereo_failure("caml_crop_unavailable")
            return None
        right = right_processor.prepare(
            frame.right_mosaic,
            refined_centroid,
            source_size_px,
            allow_padding=allow_padding,
        )
        if right is None:
            self._stereo_failure("camr_crop_unavailable")
            return None
        left_materializer, left_width, left_height, left_padded = left
        right_materializer, right_width, right_height, right_padded = right
        if (left_width, left_height) != (right_width, right_height):
            raise SourceError("CamL and CamR crop dimensions differ")
        distance = math.hypot(
            refined_centroid[0] - projected[0],
            refined_centroid[1] - projected[1],
        )
        return StereoCropPreparation(
            left_materializer,
            right_materializer,
            left_width,
            left_height,
            source_size_px,
            left_padded or right_padded,
            StereoPairMetadata(
                left_frame_index=frame.index,
                right_frame_index=pair.right_frame_index,
                left_timestamp_ns=pair.left_timestamp_ns,
                right_timestamp_ns=pair.right_timestamp_ns,
                caml_centroid_px=centroid_px,
                camr_projected_centroid_px=projected,
                camr_centroid_px=refined_centroid,
                refinement_distance_px=distance,
                refinement_area_px=area_px,
            ),
        )

    def _build_right_background(
        self, indices: tuple[int, ...]
    ) -> tuple[np.ndarray, np.ndarray]:
        if not indices:
            raise SourceError("at least one CamR background frame is required")
        frames: list[np.ndarray] = []
        fallback_frames: list[np.ndarray] = []
        stored_lut = self._right_stored_detection_lut
        if stored_lut is None:
            raise SourceError("CamR detection levels are not configured")
        for index in indices:
            pair = self._stereo_pairs.get(index)
            if pair is None:
                raise SourceError(f"background frame {index} has no CamR pair")
            mapping, mosaic, _path = _mmap_raw_mosaic(
                self.path,
                pair.right_path,
                raw_offset=pair.right_offset,
                width=self._right_width,
                height=self._right_height,
                stride=self._right_stride,
                expected_bytes=self._right_expected_bytes,
                frame_index=pair.right_frame_index,
                camera_id="CamR",
            )
            try:
                frames.append(_raw_single_green_plane(mosaic, stored_lut).copy())
                fallback_frames.append(
                    _raw_green_plane(mosaic, stored_lut).copy()
                )
            finally:
                mapping.close()
        return (
            np.median(np.stack(frames, axis=0), axis=0).astype(np.uint8),
            np.median(np.stack(fallback_frames, axis=0), axis=0).astype(np.uint8),
        )

    def _refine_right_centroid(
        self,
        frame: RawReplayFrame,
        projected_px: tuple[float, float],
        crop_size_px: int,
    ) -> tuple[tuple[float, float], int, tuple[float, float]] | None:
        background_blurred = self._right_background_blurred
        if background_blurred is None:
            raise SourceError("CamR background has not been built")
        x_px, y_px = projected_px
        if not (0 <= x_px < self._right_width and 0 <= y_px < self._right_height):
            return None
        stored_lut = self._right_stored_detection_lut
        if stored_lut is None:
            raise SourceError("CamR detection levels are not configured")
        half_extent_px = crop_size_px / 2.0 + self._stereo_search_margin_px
        native_left = max(0, math.floor(x_px - half_extent_px))
        native_right = min(self._right_width, math.ceil(x_px + half_extent_px))
        native_top = max(0, math.floor(y_px - half_extent_px))
        native_bottom = min(self._right_height, math.ceil(y_px + half_extent_px))
        # Preserve the RGGB phase and a complete 2x2 cell at every edge.
        native_left -= native_left % 2
        native_top -= native_top % 2
        native_right -= native_right % 2
        native_bottom -= native_bottom % 2
        mosaic_roi = frame.right_mosaic[
            native_top:native_bottom,
            native_left:native_right,
        ]
        current_roi = _raw_single_green_plane(mosaic_roi, stored_lut)
        result = self._foreground_component(
            current_roi,
            background_blurred,
            native_left=native_left,
            native_top=native_top,
            native_right=native_right,
            native_bottom=native_bottom,
            projected_px=projected_px,
        )
        if result is not None:
            return result

        # One native green sample avoids interpolation and halves the hot ROI
        # work. Retain averaged dual-green segmentation as a rare recovery path
        # rather than allowing a marginal spectrum/noise case to lose evidence.
        fallback_background = self._right_fallback_background_blurred
        if fallback_background is None:
            raise SourceError("CamR fallback background has not been built")
        self._stereo_refinement_fallbacks += 1
        return self._foreground_component(
            _raw_green_plane(mosaic_roi, stored_lut),
            fallback_background,
            native_left=native_left,
            native_top=native_top,
            native_right=native_right,
            native_bottom=native_bottom,
            projected_px=projected_px,
        )

    def _foreground_component(
        self,
        current_roi: np.ndarray,
        background_blurred: np.ndarray,
        *,
        native_left: int,
        native_top: int,
        native_right: int,
        native_bottom: int,
        projected_px: tuple[float, float],
    ) -> tuple[tuple[float, float], int, tuple[float, float]] | None:
        left = native_left // 2
        right = native_right // 2
        top = native_top // 2
        bottom = native_bottom // 2
        if right - left < 3 or bottom - top < 3:
            return None
        blurred = cv2.GaussianBlur(current_roi, (5, 5), 0)
        difference = cv2.absdiff(
            blurred, background_blurred[top:bottom, left:right]
        )
        _unused, foreground = cv2.threshold(
            difference,
            self._stereo_refinement_threshold,
            255,
            cv2.THRESH_BINARY,
        )
        foreground = cv2.morphologyEx(
            foreground,
            cv2.MORPH_CLOSE,
            self._stereo_close_kernel,
            iterations=1,
        )
        foreground = cv2.morphologyEx(
            foreground,
            cv2.MORPH_OPEN,
            self._stereo_open_kernel,
            iterations=1,
        )
        contours, _hierarchy = cv2.findContours(
            foreground,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        scale_x = current_roi.shape[1] / (native_right - native_left)
        scale_y = current_roi.shape[0] / (native_bottom - native_top)
        x_px, y_px = projected_px
        candidates: list[
            tuple[float, tuple[float, float], int, tuple[float, float]]
        ] = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            component = foreground[y : y + height, x : x + width]
            moments = cv2.moments(component, binaryImage=True)
            if moments["m00"] <= 0:
                continue
            native_width = width / scale_x
            native_height = height / scale_y
            native_area = round(moments["m00"] / (scale_x * scale_y))
            if (
                native_area < 600
                or native_area > 80_000
                or native_width < 20
                or native_height < 20
                or native_width > 360
                or native_height > 360
            ):
                continue
            centre = (
                float(
                    native_left
                    + (x + moments["m10"] / moments["m00"]) / scale_x
                ),
                float(
                    native_top
                    + (y + moments["m01"] / moments["m00"]) / scale_y
                ),
            )
            distance = math.hypot(centre[0] - x_px, centre[1] - y_px)
            if distance <= self._stereo_max_refinement_px:
                candidates.append(
                    (
                        distance,
                        centre,
                        native_area,
                        (native_width, native_height),
                    )
                )
        if not candidates:
            return None
        _distance, centre, area, component_size = min(
            candidates, key=lambda item: item[0]
        )
        return centre, area, component_size

    def undistort_point(self, point: tuple[float, float]) -> tuple[float, float]:
        return self.undistort_points((point,))[0]

    def undistort_points(
        self, points: tuple[tuple[float, float], ...]
    ) -> tuple[tuple[float, float], ...]:
        if not points:
            return ()
        values = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
        mapped = cv2.undistortPoints(
            values,
            self._camera_matrix,
            self._distortion,
            P=self._camera_matrix,
        ).reshape(-1, 2)
        return tuple((float(x), float(y)) for x, y in mapped)

    def preview_frame(self, frame: RawReplayFrame) -> np.ndarray:
        gray = cv2.resize(
            frame.detection_gray,
            (self._width, self._height),
            interpolation=cv2.INTER_LINEAR,
        )
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def release_frame(self, frame) -> None:
        if isinstance(frame, RawReplayFrame):
            self._active.pop(id(frame), None)
            frame.close()

    def close(self) -> None:
        for frame in tuple(self._active.values()):
            frame.close()
        self._active.clear()

    def __enter__(self) -> MMapRawVideoSource:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _row(self, index: int) -> tuple[int, Path, int]:
        if not 0 <= index < len(self._rows):
            raise SourceError(f"frame {index} is outside the RAW bundle")
        return self._rows[index]


class RawCropProcessor:
    """Produce either model-oriented or calibrated crops from a Bayer ROI."""

    _CFA_BY_OFFSET: ClassVar[dict[tuple[int, int], str]] = {
        (0, 0): "RGGB",
        (0, 1): "GRBG",
        (1, 0): "GBRG",
        (1, 1): "BGGR",
    }

    def __init__(
        self,
        profile_path: Path,
        profile: dict[str, object],
        *,
        processing_profile: str = "ml-fast",
    ) -> None:
        if processing_profile not in {"ml-fast", "calibrated"}:
            raise SourceError(
                "RAW crop processing must be 'ml-fast' or 'calibrated'"
            )
        capture = profile["capture"]
        calibration = profile["calibration"]
        processing = profile.get("processing", {})
        self.processing_profile = processing_profile
        self._bit_shift = int(capture.get("bit_shift", 0))
        self._white_level = float(capture["decoded_white_level"])
        self._dark_level = float(calibration.get("dark_level_median", 0.0))
        levels = np.arange(round(self._white_level) + 1, dtype=np.float32)
        self._ml_lut = np.clip(
            levels * (255.0 / max(self._white_level, 1.0)) + 0.5,
            0,
            255,
        ).astype(np.uint8)
        self._wb = (
            np.asarray(calibration["wb_gains_rgb"], dtype=np.float32)
            if calibration.get("wb_enabled", False)
            else None
        )
        self._matrix = (
            np.asarray(calibration["color_matrix_rgb"], dtype=np.float32)
            if calibration.get("color_matrix_enabled", False)
            else None
        )
        self._algorithm = str(processing.get("demosaic", "edge_aware"))
        self._dark = None
        self._flat = None
        self._defects = None
        if processing_profile == "ml-fast":
            return

        artifacts = profile["artifacts"]
        expected_shape = (int(capture["height"]), int(capture["width"]))
        root = profile_path.parent

        def artifact(name: str) -> np.ndarray:
            descriptor = artifacts[name]
            path = (root / descriptor["path"]).resolve()
            if root not in path.parents:
                raise SourceError(f"calibration artifact escapes CamL pack: {name}")
            return np.load(path, mmap_mode="r")

        self._dark = artifact("master_dark")
        self._flat = artifact("flat_gain")
        defects = artifact("defect_map")
        if any(
            value.shape != expected_shape for value in (self._dark, self._flat, defects)
        ):
            raise SourceError("CamL calibration artifact dimensions do not match RAW")
        self._defects = defects.astype(bool)

    def extract(
        self,
        mosaic: np.ndarray,
        centroid_px: tuple[float, float],
        size_px: int,
        *,
        allow_padding: bool,
    ) -> tuple[np.ndarray | None, bool]:
        prepared = self.prepare(
            mosaic,
            centroid_px,
            size_px,
            allow_padding=allow_padding,
        )
        if prepared is None:
            return None, False
        materialize, _width, _height, padded = prepared
        return materialize(), padded

    def prepare(
        self,
        mosaic: np.ndarray,
        centroid_px: tuple[float, float],
        size_px: int,
        *,
        allow_padding: bool,
    ) -> tuple[Callable[[], np.ndarray], int, int, bool] | None:
        if size_px <= 0:
            raise ValueError("crop size must be positive")
        centre_x, centre_y = round(centroid_px[0]), round(centroid_px[1])
        left = centre_x - size_px // 2
        top = centre_y - size_px // 2
        right = left + size_px
        bottom = top + size_px
        height, width = mosaic.shape
        complete = left >= 0 and top >= 0 and right <= width and bottom <= height
        if not complete and not allow_padding:
            return None
        source_left = max(0, left)
        source_top = max(0, top)
        source_right = min(width, right)
        source_bottom = min(height, bottom)
        if source_left >= source_right or source_top >= source_bottom:
            return None

        halo = 4
        roi_left = max(0, source_left - halo)
        roi_top = max(0, source_top - halo)
        roi_right = min(width, source_right + halo)
        roi_bottom = min(height, source_bottom + halo)
        raw = np.ascontiguousarray(mosaic[roi_top:roi_bottom, roi_left:roi_right])

        def materialize() -> np.ndarray:
            return self._process(
                raw,
                roi_left=roi_left,
                roi_top=roi_top,
                source_left=source_left,
                source_top=source_top,
                source_right=source_right,
                source_bottom=source_bottom,
                crop_left=left,
                crop_top=top,
                crop_size=size_px,
                padded=not complete,
            )

        return materialize, size_px, size_px, not complete

    def _process(
        self,
        raw: np.ndarray,
        *,
        roi_left: int,
        roi_top: int,
        source_left: int,
        source_top: int,
        source_right: int,
        source_bottom: int,
        crop_left: int,
        crop_top: int,
        crop_size: int,
        padded: bool,
    ) -> np.ndarray:
        roi_bottom = roi_top + raw.shape[0]
        roi_right = roi_left + raw.shape[1]
        pattern = self._CFA_BY_OFFSET[(roi_top % 2, roi_left % 2)]
        if self.processing_profile == "ml-fast":
            decoded = (
                np.right_shift(raw, self._bit_shift)
                if self._bit_shift
                else raw
            )
            decoded = np.minimum(decoded, len(self._ml_lut) - 1)
            bayer8 = np.ascontiguousarray(self._ml_lut[decoded])
            bgr = cv2.cvtColor(
                bayer8, getattr(cv2, f"COLOR_Bayer{pattern}2BGR")
            )
            return _finish_raw_crop(
                bgr,
                roi_left=roi_left,
                roi_top=roi_top,
                source_left=source_left,
                source_top=source_top,
                source_right=source_right,
                source_bottom=source_bottom,
                crop_left=crop_left,
                crop_top=crop_top,
                crop_size=crop_size,
                padded=padded,
            )

        if self._dark is None or self._flat is None or self._defects is None:
            raise SourceError("calibrated RAW crop artifacts are unavailable")
        decoded = (
            np.right_shift(raw, self._bit_shift).astype(np.float32)
            if self._bit_shift
            else raw.astype(np.float32)
        )
        decoded -= self._dark[roi_top:roi_bottom, roi_left:roi_right]
        defect = self._defects[roi_top:roi_bottom, roi_left:roi_right]
        if np.any(defect):
            for row in (0, 1):
                for column in (0, 1):
                    plane = decoded[row::2, column::2]
                    plane_mask = defect[row::2, column::2]
                    if np.any(plane_mask):
                        median = cv2.medianBlur(plane, 3)
                        plane[plane_mask] = median[plane_mask]
        decoded *= self._flat[roi_top:roi_bottom, roi_left:roi_right]
        suffix = "_EA" if self._algorithm == "edge_aware" else ""
        code_name = f"COLOR_Bayer{pattern}2RGB{suffix}"
        code = getattr(cv2, code_name, getattr(cv2, f"COLOR_Bayer{pattern}2RGB"))
        rgb = cv2.cvtColor(
            np.clip(decoded, 0, 65535).round().astype(np.uint16), code
        ).astype(np.float32)
        rgb /= max(self._white_level - self._dark_level, 1.0)
        if self._wb is not None:
            rgb *= self._wb.reshape(1, 1, 3)
        if self._matrix is not None:
            rgb = np.einsum("...c,dc->...d", rgb, self._matrix)
        rgb = np.clip(rgb, 0.0, 1.0)
        rgb = np.where(
            rgb <= 0.0031308,
            rgb * 12.92,
            1.055 * np.power(rgb, 1.0 / 2.4) - 0.055,
        )
        rgb8 = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
        return _finish_raw_crop(
            rgb8[..., ::-1],
            roi_left=roi_left,
            roi_top=roi_top,
            source_left=source_left,
            source_top=source_top,
            source_right=source_right,
            source_bottom=source_bottom,
            crop_left=crop_left,
            crop_top=crop_top,
            crop_size=crop_size,
            padded=padded,
        )


def _finish_raw_crop(
    bgr: np.ndarray,
    *,
    roi_left: int,
    roi_top: int,
    source_left: int,
    source_top: int,
    source_right: int,
    source_bottom: int,
    crop_left: int,
    crop_top: int,
    crop_size: int,
    padded: bool,
) -> np.ndarray:
    crop = bgr[
        source_top - roi_top : source_bottom - roi_top,
        source_left - roi_left : source_right - roi_left,
    ]
    if not padded:
        return np.ascontiguousarray(crop)
    output = np.zeros((crop_size, crop_size, 3), dtype=np.uint8)
    output[
        source_top - crop_top : source_bottom - crop_top,
        source_left - crop_left : source_right - crop_left,
    ] = crop
    return output


def _stored_value_lut(decoded_lut: np.ndarray, bit_shift: int) -> np.ndarray:
    stored = np.arange(1 << 16, dtype=np.uint32)
    decoded = np.right_shift(stored, bit_shift) if bit_shift else stored
    return decoded_lut[np.minimum(decoded, len(decoded_lut) - 1)]


def _raw_single_green_plane(
    mosaic: np.ndarray,
    stored_lut: np.ndarray,
) -> np.ndarray:
    return np.ascontiguousarray(stored_lut[mosaic[0::2, 1::2]])


def _raw_green_plane(
    mosaic: np.ndarray,
    stored_lut: np.ndarray,
) -> np.ndarray:
    green_stored = cv2.addWeighted(
        mosaic[0::2, 1::2], 0.5, mosaic[1::2, 0::2], 0.5, 0.0
    )
    return np.ascontiguousarray(stored_lut[green_stored])


def _mmap_raw_mosaic(
    root: Path,
    relative: Path,
    *,
    raw_offset: int,
    width: int,
    height: int,
    stride: int,
    expected_bytes: int,
    frame_index: int,
    camera_id: str,
) -> tuple[mmap.mmap, np.ndarray, Path]:
    path = (root / relative).resolve()
    if root not in path.parents:
        raise SourceError(f"{camera_id} RAW path escapes recording bundle: {relative}")
    try:
        descriptor = path.stat()
    except OSError as exc:
        raise SourceError(
            f"cannot stat {camera_id} RAW frame {frame_index + 1}: {exc}"
        ) from exc
    if raw_offset < 0:
        raise SourceError(
            f"{camera_id} RAW frame {frame_index + 1} has invalid offset {raw_offset}"
        )
    if descriptor.st_size < raw_offset + expected_bytes:
        raise SourceError(
            f"{camera_id} RAW frame {frame_index + 1} exceeds its "
            f"{descriptor.st_size}-byte source at offset {raw_offset}"
        )
    descriptor_fd = os.open(path, os.O_RDONLY)
    mapping_offset = raw_offset - raw_offset % mmap.ALLOCATIONGRANULARITY
    buffer_offset = raw_offset - mapping_offset
    try:
        mapping = mmap.mmap(
            descriptor_fd,
            buffer_offset + expected_bytes,
            access=mmap.ACCESS_READ,
            offset=mapping_offset,
        )
    finally:
        os.close(descriptor_fd)
    try:
        words = np.ndarray(
            (height, stride // 2),
            dtype="<u2",
            buffer=mapping,
            offset=buffer_offset,
        )
        mosaic = words[:, :width]
    except Exception:
        mapping.close()
        raise
    return mapping, mosaic, path


def resolve_raw_bundle(path: Path) -> Path:
    """Resolve a bundle root from the root itself or its CamL derivative."""

    resolved = path.expanduser().resolve()
    candidates = [resolved] if resolved.is_dir() else []
    if resolved.is_file():
        candidates.extend((resolved.parent, resolved.parent.parent))
    for candidate in candidates:
        if (
            (candidate / "metadata/CamL.csv").is_file()
            and (candidate / "calibration/CamL/profile.json").is_file()
            and (candidate / "recording.json").is_file()
        ):
            return candidate
    raise SourceError("recording has no complete CamL RAW bundle")


def find_raw_bundle(path: Path) -> Path | None:
    try:
        return resolve_raw_bundle(path)
    except SourceError:
        return None


def open_replay_source(
    path: Path, *, prefer_raw: bool = False, cache_frames: int = 2
) -> ReplaySource:
    resolved = path.expanduser().resolve()
    if resolved.is_dir() and prefer_raw:
        return RawBundleVideoSource(resolved, cache_frames=cache_frames)
    try:
        return RecordingVideoSource(resolved, cache_frames=cache_frames)
    except SourceError:
        if resolved.is_dir() and (resolved / "metadata/CamL.csv").is_file():
            return RawBundleVideoSource(resolved, cache_frames=cache_frames)
        raise


def _read_pair_timestamps(path: Path) -> list[int]:
    if not path.is_file():
        return []
    values: list[int] = []
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                values.append(int(row["left_timestamp_ns"]))
    except (OSError, ValueError, KeyError):
        return []
    if any(later <= earlier for earlier, later in pairwise(values)):
        return []
    return values


def _read_stereo_pairs(path: Path) -> tuple[_RawStereoPair, ...]:
    if not path.is_file():
        return ()
    rows: list[_RawStereoPair] = []
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                rows.append(
                    _RawStereoPair(
                        left_frame_index=int(row["left_frame_index"]),
                        right_frame_index=int(row["right_frame_index"]),
                        left_timestamp_ns=int(row["left_timestamp_ns"]),
                        right_timestamp_ns=int(row["right_timestamp_ns"]),
                        left_path=Path(row["left_raw_path"]),
                        right_path=Path(row["right_raw_path"]),
                        left_offset=int(row.get("left_raw_offset") or 0),
                        right_offset=int(row.get("right_raw_offset") or 0),
                    )
                )
    except (OSError, ValueError, KeyError):
        return ()
    if len({item.left_frame_index for item in rows}) != len(rows):
        return ()
    return tuple(rows)


def _read_raw_metadata(path: Path) -> tuple[tuple[int, Path, int], ...]:
    rows: list[tuple[int, Path, int]] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for expected_index, row in enumerate(csv.DictReader(stream)):
            index = int(row["frame_index"])
            timestamp = int(row["timestamp_ns"])
            relative = Path(row["raw_path"])
            raw_offset = int(row.get("raw_offset") or 0)
            if index != expected_index:
                raise ValueError("CamL RAW metadata frame indices are not contiguous")
            if rows and timestamp <= rows[-1][0]:
                raise ValueError("CamL RAW timestamps are not strictly increasing")
            if raw_offset < 0:
                raise ValueError("RAW metadata offsets must be non-negative")
            rows.append((timestamp, relative, raw_offset))
    return tuple(rows)


def _read_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return value


def _fastcap_calibration_types():
    try:
        from beanofastcap.calibration import CalibrationProcessor
        from beanofastcap.calibration_pack import CalibrationPack

        return CalibrationPack, CalibrationProcessor
    except ImportError:
        sibling_source = Path(__file__).resolve().parents[3] / "BeanoFastCap/src"
        if sibling_source.is_dir() and str(sibling_source) not in sys.path:
            sys.path.insert(0, str(sibling_source))
        try:
            from beanofastcap.calibration import CalibrationProcessor
            from beanofastcap.calibration_pack import CalibrationPack

            return CalibrationPack, CalibrationProcessor
        except ImportError as exc:
            raise SourceError(
                "RAW replay needs BeanoFastCap's calibration package or a sibling "
                "BeanoFastCap source tree"
            ) from exc
