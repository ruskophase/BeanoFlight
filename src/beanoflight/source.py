"""CamL FFV1 input with exact FastCap timestamp sidecar support."""

from __future__ import annotations

import csv
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

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
        self.path = resolve_caml_video(path)
        self._cache_limit = max(1, cache_frames)
        self._cache: OrderedDict[int, object] = OrderedDict()
        self._capture = self._open()
        self._next_index = 0
        width = int(round(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
        height = int(round(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        frame_count = int(round(self._capture.get(cv2.CAP_PROP_FRAME_COUNT)))
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
            self._timestamps = tuple(round(index * period_ns) for index in range(frame_count))
        self.metadata = SourceMetadata(self.path, width, height, frame_count, fps, exact)

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
        observed = int(round(self._capture.get(cv2.CAP_PROP_POS_FRAMES)))
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

    def clone(self) -> "RecordingVideoSource":
        return RecordingVideoSource(self.path, cache_frames=1)

    def close(self) -> None:
        capture = getattr(self, "_capture", None)
        if capture is not None:
            capture.release()
        self._capture = None
        self._cache.clear()

    def __enter__(self) -> "RecordingVideoSource":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


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
    if any(later <= earlier for earlier, later in zip(values, values[1:])):
        return []
    return values
