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

    @property
    def mosaic(self) -> np.ndarray:
        if self._mosaic is None:
            raise SourceError("RAW replay frame has already been released")
        return self._mosaic

    def close(self) -> None:
        self._mosaic = None
        mapping = self._mapping
        self._mapping = None
        if mapping is not None:
            mapping.close()


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
        self._crop_processor = RawCropProcessor(
            profile_path, profile, processing_profile=crop_processing
        )
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
        }

    def timestamp_ns(self, index: int) -> int:
        return self._row(index)[0]

    def frame(self, index: int) -> RawReplayFrame:
        _timestamp, relative = self._row(index)
        path = (self.path / relative).resolve()
        if self.path not in path.parents:
            raise SourceError(f"RAW frame path escapes recording bundle: {relative}")
        try:
            descriptor = path.stat()
        except OSError as exc:
            raise SourceError(f"cannot stat RAW frame {index + 1}: {exc}") from exc
        if descriptor.st_size != self._expected_bytes:
            raise SourceError(
                f"RAW frame {index + 1} has {descriptor.st_size} bytes; "
                f"expected {self._expected_bytes}"
            )
        descriptor_fd = os.open(path, os.O_RDONLY)
        try:
            mapping = mmap.mmap(
                descriptor_fd, self._expected_bytes, access=mmap.ACCESS_READ
            )
        except Exception:
            os.close(descriptor_fd)
            raise
        os.close(descriptor_fd)
        try:
            words = np.ndarray(
                (self._height, self._stride // 2), dtype="<u2", buffer=mapping
            )
            mosaic = words[:, : self._width]
            green_stored = cv2.addWeighted(
                mosaic[0::2, 1::2], 0.5, mosaic[1::2, 0::2], 0.5, 0.0
            )
            decoded = (
                np.right_shift(green_stored, self._bit_shift)
                if self._bit_shift
                else green_stored
            )
            decoded = np.minimum(decoded, len(self._detection_lut) - 1)
            detection_gray = np.ascontiguousarray(self._detection_lut[decoded])
            frame = RawReplayFrame(
                index,
                path,
                detection_gray,
                (self._width, self._height),
                mapping,
                mosaic,
            )
        except Exception:
            mapping.close()
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

    def _row(self, index: int) -> tuple[int, Path]:
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


def _read_raw_metadata(path: Path) -> tuple[tuple[int, Path], ...]:
    rows: list[tuple[int, Path]] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for expected_index, row in enumerate(csv.DictReader(stream)):
            index = int(row["frame_index"])
            timestamp = int(row["timestamp_ns"])
            relative = Path(row["raw_path"])
            if index != expected_index:
                raise ValueError("CamL RAW metadata frame indices are not contiguous")
            if rows and timestamp <= rows[-1][0]:
                raise ValueError("CamL RAW timestamps are not strictly increasing")
            rows.append((timestamp, relative))
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
