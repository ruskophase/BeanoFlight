"""CamL FFV1 input with exact FastCap timestamp sidecar support."""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import OrderedDict
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import cv2

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

    def __enter__(self) -> RawBundleVideoSource:  # noqa: PYI034 - Python 3.10
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _row(self, index: int) -> tuple[int, Path]:
        if not 0 <= index < len(self._rows):
            raise SourceError(f"frame {index} is outside the RAW bundle")
        return self._rows[index]


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
