"""Direct synchronized RAW camera input supplied by headless BeanoFastCap."""

from __future__ import annotations

import json
import mmap
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .source import (
    MMapRawVideoSource,
    RawCropProcessor,
    RawReplayFrame,
    SourceError,
    SourceMetadata,
    _raw_green_plane,
    _raw_single_green_plane,
    _RawStereoPair,
    _stored_value_lut,
)
from .stereo import StereoPointCalibration

_PREVIEW_HEADER = struct.Struct("<8s6I4Q")
_PREVIEW_MAGIC = b"BFCAP01"
_PREVIEW_VERSION = 1
_PREVIEW_HEADER_BYTES = 64
_GENERATION_OFFSET = 32


@dataclass(frozen=True, slots=True)
class SharedRawSnapshot:
    sequence: int
    timestamp_ns: int
    payload: bytes


class SharedRawRegion:
    """Seqlock reader for one FastCap latest-frame shared-memory region."""

    def __init__(self, path: Path, *, timeout_seconds: float = 10.0) -> None:
        self.path = path.expanduser().resolve()
        self._mapping: mmap.mmap | None = None
        self.width = 0
        self.height = 0
        self.stride = 0
        self.frame_bytes = 0
        deadline = time.monotonic() + timeout_seconds
        last_error = "shared-memory file has not appeared"
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.01)
                continue
            try:
                size = os.fstat(descriptor).st_size
                if size < _PREVIEW_HEADER_BYTES:
                    last_error = f"region is only {size} bytes"
                    time.sleep(0.01)
                    continue
                mapping = mmap.mmap(descriptor, size, access=mmap.ACCESS_READ)
            finally:
                os.close(descriptor)
            try:
                values = _PREVIEW_HEADER.unpack_from(mapping)
                magic = values[0].rstrip(b"\0")
                version, header_bytes, width, height, stride, frame_bytes = values[1:7]
                if magic != _PREVIEW_MAGIC:
                    last_error = f"unexpected shared-memory magic {magic!r}"
                    mapping.close()
                    time.sleep(0.01)
                    continue
                if version != _PREVIEW_VERSION or header_bytes != _PREVIEW_HEADER_BYTES:
                    raise SourceError(
                        f"unsupported FastCap shared-memory layout v{version}/{header_bytes}"
                    )
                if (
                    width <= 0
                    or height <= 0
                    or stride < width * 2
                    or frame_bytes != height * stride
                    or size != header_bytes + frame_bytes
                ):
                    raise SourceError("FastCap shared-memory geometry is invalid")
                self._mapping = mapping
                self.width = int(width)
                self.height = int(height)
                self.stride = int(stride)
                self.frame_bytes = int(frame_bytes)
                return
            except Exception:
                if not mapping.closed:
                    mapping.close()
                raise
        raise SourceError(f"cannot open {self.path}: {last_error}")

    def latest_after(self, sequence: int | None) -> SharedRawSnapshot | None:
        mapping = self._mapping
        if mapping is None:
            raise SourceError(f"shared-memory region is closed: {self.path}")
        for _attempt in range(4):
            generation_before = struct.unpack_from(
                "<Q", mapping, _GENERATION_OFFSET
            )[0]
            if generation_before == 0 or generation_before & 1:
                continue
            values = _PREVIEW_HEADER.unpack_from(mapping)
            current_sequence = int(values[8])
            timestamp_ns = int(values[9])
            bytes_used = int(values[10])
            if sequence is not None and current_sequence == sequence:
                return None
            if bytes_used != self.frame_bytes or timestamp_ns <= 0:
                continue
            payload = bytes(
                mapping[
                    _PREVIEW_HEADER_BYTES : _PREVIEW_HEADER_BYTES + self.frame_bytes
                ]
            )
            generation_after = struct.unpack_from(
                "<Q", mapping, _GENERATION_OFFSET
            )[0]
            if generation_before == generation_after and not generation_after & 1:
                return SharedRawSnapshot(current_sequence, timestamp_ns, payload)
        return None

    def close(self) -> None:
        mapping = self._mapping
        self._mapping = None
        if mapping is not None:
            mapping.close()


class FastCapLiveProducer:
    """Own the fail-closed FastCap subprocess feeding two shared regions."""

    def __init__(
        self,
        calibration_pack: Path,
        state_root: Path,
        preview_paths: dict[str, Path],
        *,
        duration_seconds: float,
        controller_url: str | None = None,
        witness_result: Path | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.calibration_pack = calibration_pack.expanduser().resolve()
        self.state_root = state_root.expanduser().resolve()
        self.preview_paths = {
            name: path.expanduser().resolve() for name, path in preview_paths.items()
        }
        self.duration_seconds = duration_seconds
        self.controller_url = controller_url
        self.witness_result = witness_result
        self.event_callback = event_callback or (lambda _event: None)
        self.process: subprocess.Popen[str] | None = None
        self.clock_source_timestamp_ns = 0
        self.clock_monotonic_ns = 0
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    def start(self, *, timeout_seconds: float = 15.0) -> None:
        if self.process is not None:
            raise SourceError("FastCap live producer has already been started")
        fastcap_source = Path(__file__).resolve().parents[3] / "BeanoFastCap/src"
        environment = dict(os.environ)
        existing_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(fastcap_source) + (
            os.pathsep + existing_path if existing_path else ""
        )
        command = [
            sys.executable,
            "-m",
            "beanofastcap",
            "live",
            "--calibration-pack",
            str(self.calibration_pack),
            "--output",
            str(self.state_root),
            "--duration",
            str(self.duration_seconds),
            "--left-shm",
            str(self.preview_paths["CamL"]),
            "--right-shm",
            str(self.preview_paths["CamR"]),
        ]
        if self.controller_url:
            command.extend(("--controller-url", self.controller_url))
        if self.witness_result is not None:
            command.extend(("--witness-result", str(self.witness_result)))
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.check_running()
            try:
                event = self._events.get(timeout=0.05)
            except queue.Empty:
                continue
            self.event_callback(event)
            if event.get("event") == "synchronized":
                self.clock_source_timestamp_ns = int(event["source_timestamp_ns"])
                self.clock_monotonic_ns = int(event["anchor_monotonic_ns"])
                return
        self.close()
        raise SourceError("FastCap did not synchronize both live cameras within timeout")

    def check_running(self) -> None:
        process = self.process
        if process is None:
            raise SourceError("FastCap live producer is not running")
        code = process.poll()
        if code is None:
            return
        detail = "".join(self._stderr).strip()
        raise SourceError(
            f"FastCap live producer exited with status {code}"
            + (f": {detail}" if detail else "")
        )

    def drain_events(self) -> None:
        while True:
            try:
                self.event_callback(self._events.get_nowait())
            except queue.Empty:
                return

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        for reader in (self._reader, self._stderr_reader):
            if reader is not None:
                reader.join(timeout=1.0)
        self.drain_events()

    def _read_events(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = {"event": "fastcap_output", "message": line.rstrip()}
            if isinstance(value, dict):
                self._events.put(value)

    def _read_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        for line in process.stderr:
            self._stderr.append(line)


class SharedMemoryRawStereoSource(MMapRawVideoSource):
    """Sequential live stereo source backed by two latest-frame regions."""

    source_kind = "live-shared-rg10-green"
    live = True

    def __init__(
        self,
        calibration_pack: Path,
        preview_paths: dict[str, Path],
        *,
        frame_count: int,
        fps: float,
        clock_source_timestamp_ns: int,
        clock_monotonic_ns: int,
        pair_threshold_us: float = 5.0,
        crop_processing: str = "ml-fast",
        producer_check: Callable[[], None] | None = None,
    ) -> None:
        if frame_count <= 0 or fps <= 0 or pair_threshold_us < 0:
            raise SourceError("live frame count, FPS and pair threshold are invalid")
        root = calibration_pack.expanduser().resolve()
        left_profile_path = root / "CamL/profile.json"
        right_profile_path = root / "CamR/profile.json"
        homography_path = root / "geometry/homography.json"
        try:
            left_profile = _read_json(left_profile_path)
            right_profile = _read_json(right_profile_path)
            left = _capture_geometry(left_profile, "CamL")
            right = _capture_geometry(right_profile, "CamR")
            point_calibration = StereoPointCalibration.load(
                homography_path, left_profile, right_profile
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceError(f"cannot configure live RAW calibration: {exc}") from exc
        if left[0:3] != right[0:3]:
            raise SourceError("live CamL and CamR capture geometry differs")

        self.path = root
        self.profile_path = left_profile_path
        self.profile = left_profile
        self._rows: tuple[tuple[int, Path, int], ...] = ()
        self._width, self._height, self._stride = left[0:3]
        self._bit_shift = left[3]
        self._expected_bytes = self._height * self._stride
        self._active: dict[int, RawReplayFrame] = {}
        left_calibration = left_profile["calibration"]
        self._camera_matrix = np.asarray(
            left_calibration["camera_matrix"], dtype=np.float64
        )
        self._distortion = np.asarray(
            left_calibration["distortion_coefficients"], dtype=np.float64
        )
        self._detection_lut = _signal_lut(left[4], left[5])
        self._stored_detection_lut = _stored_value_lut(
            self._detection_lut, self._bit_shift
        )
        self._crop_processor = RawCropProcessor(
            left_profile_path,
            left_profile,
            processing_profile=crop_processing,
        )
        self.crop_processing_profile = self._crop_processor.processing_profile

        self._stereo_calibration = point_calibration
        self._stereo_pairs: dict[int, _RawStereoPair] = {}
        self._right_profile = right_profile
        self._right_rows: tuple[tuple[int, Path, int], ...] = ()
        self._right_width, self._right_height, self._right_stride = right[0:3]
        self._right_bit_shift = right[3]
        self._right_expected_bytes = self._right_height * self._right_stride
        self._right_detection_lut = _signal_lut(right[4], right[5])
        self._right_stored_detection_lut = _stored_value_lut(
            self._right_detection_lut, self._right_bit_shift
        )
        self._right_crop_processor = RawCropProcessor(
            right_profile_path,
            right_profile,
            processing_profile=crop_processing,
        )
        self._right_background: np.ndarray | None = None
        self._right_background_blurred: np.ndarray | None = None
        self._right_fallback_background_blurred: np.ndarray | None = None
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

        self._regions = {
            name: SharedRawRegion(preview_paths[name]) for name in ("CamL", "CamR")
        }
        for name, region in self._regions.items():
            expected = left[0:3] if name == "CamL" else right[0:3]
            if (region.width, region.height, region.stride) != expected:
                self.close()
                raise SourceError(
                    f"{name} live geometry disagrees with calibration pack"
                )
        self.clock_source_timestamp_ns = int(clock_source_timestamp_ns)
        self.clock_monotonic_ns = int(clock_monotonic_ns)
        self._pair_threshold_ns = round(pair_threshold_us * 1_000)
        self._producer_check = producer_check or (lambda: None)
        self._candidate_sequences: dict[str, int | None] = {"CamL": None, "CamR": None}
        self._last_delivered_sequences: dict[str, int | None] = {
            "CamL": None,
            "CamR": None,
        }
        self._timestamps: list[int] = []
        self._pending: tuple[SharedRawSnapshot, SharedRawSnapshot] | None = None
        self._next_index = 0
        self._sequence_drops = {"CamL": 0, "CamR": 0}
        self._unmatched = {"CamL": 0, "CamR": 0}
        self._pair_skews_ns: list[int] = []
        self._background_samples = 0
        self.metadata = SourceMetadata(root, self._width, self._height, frame_count, fps, True)
        self.pipeline_metadata = {
            "input": "live FastCap shared-memory RG10",
            "recording_or_playback": False,
            "detection": f"{self._width // 2}x{self._height // 2} sRGB green plane",
            "colour": (
                "linear sensor BGR inference crops"
                if self.crop_processing_profile == "ml-fast"
                else "calibrated sRGB inference crops"
            ),
            "crop_processing": self.crop_processing_profile,
            "pixel_coordinate_domain": "distorted RAW",
            "metric_coordinate_domain": "point-undistorted PinkPlane",
            "stereo": "synchronized live CamL/CamR RAW ROI pairs",
            "stereo_homography": str(homography_path),
            "stereo_max_refinement_px": self._stereo_max_refinement_px,
            "bounded_latest_frame": True,
        }

    def acquire_background(
        self,
        sample_count: int,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> np.ndarray:
        if sample_count < 3:
            raise SourceError("live background requires at least three frame pairs")
        left_frames: list[np.ndarray] = []
        right_frames: list[np.ndarray] = []
        right_fallback_frames: list[np.ndarray] = []
        for number in range(sample_count):
            left, right = self._next_pair()
            left_mosaic = self._mosaic(left, "CamL")
            right_mosaic = self._mosaic(right, "CamR")
            left_frames.append(
                _raw_green_plane(left_mosaic, self._stored_detection_lut)
            )
            right_frames.append(
                _raw_single_green_plane(
                    right_mosaic, self._right_stored_detection_lut
                )
            )
            right_fallback_frames.append(
                _raw_green_plane(right_mosaic, self._right_stored_detection_lut)
            )
            if on_progress is not None:
                on_progress(number + 1, sample_count)
        background = np.median(np.stack(left_frames), axis=0).astype(np.uint8)
        self._right_background = np.median(
            np.stack(right_frames), axis=0
        ).astype(np.uint8)
        fallback = np.median(np.stack(right_fallback_frames), axis=0).astype(
            np.uint8
        )
        self._right_background_blurred = cv2.GaussianBlur(
            self._right_background, (5, 5), 0
        )
        self._right_fallback_background_blurred = cv2.GaussianBlur(
            fallback, (5, 5), 0
        )
        self._background_samples = sample_count
        self.pipeline_metadata["background"] = {
            "method": "live synchronized temporal median",
            "sample_count": sample_count,
        }
        return background

    def prime(self) -> None:
        if self._right_background_blurred is None:
            raise SourceError("acquire the empty live background before starting")
        if self._pending is not None:
            return
        self._pending = self._next_pair()
        left, right = self._pending
        self._last_delivered_sequences = {
            "CamL": left.sequence,
            "CamR": right.sequence,
        }
        self._timestamps.append(left.timestamp_ns)

    def timestamp_ns(self, index: int) -> int:
        if not 0 <= index < self.metadata.frame_count:
            raise SourceError(f"live frame {index} is outside the configured run")
        if index >= len(self._timestamps):
            raise SourceError(f"live frame {index} has not arrived yet")
        return self._timestamps[index]

    def frame(self, index: int) -> RawReplayFrame:
        if index != self._next_index:
            raise SourceError(
                f"live source requires sequential frames; expected {self._next_index}, got {index}"
            )
        if index == 0:
            if self._pending is None:
                raise SourceError("live source must be primed before frame zero")
            left, right = self._pending
            self._pending = None
        else:
            left, right = self._next_pair()
            self._record_gaps(left, right)
            self._timestamps.append(left.timestamp_ns)
        self._pair_skews_ns.append(abs(right.timestamp_ns - left.timestamp_ns))
        left_mosaic = self._mosaic(left, "CamL")
        right_mosaic = self._mosaic(right, "CamR")
        detection_gray = _raw_green_plane(left_mosaic, self._stored_detection_lut)
        left_path = Path(f"live/CamL/{left.sequence:010d}.raw")
        right_path = Path(f"live/CamR/{right.sequence:010d}.raw")
        pair = _RawStereoPair(
            index,
            right.sequence,
            left.timestamp_ns,
            right.timestamp_ns,
            left_path,
            right_path,
        )
        self._stereo_pairs[index] = pair
        frame = RawReplayFrame(
            index=index,
            path=left_path,
            detection_gray=detection_gray,
            native_size_px=(self._width, self._height),
            _mapping=None,
            _mosaic=left_mosaic,
            right_frame_index=right.sequence,
            right_timestamp_ns=right.timestamp_ns,
            right_path=right_path,
            _right_mapping=None,
            _right_mosaic=right_mosaic,
        )
        self._active[id(frame)] = frame
        self._next_index += 1
        return frame

    def stereo_statistics(self) -> dict[str, object]:
        base = super().stereo_statistics()
        return {
            **base,
            "transport": "FastCap shared-memory latest-frame v1",
            "background_samples": self._background_samples,
            "sequence_drops": dict(self._sequence_drops),
            "unmatched_frames": dict(self._unmatched),
            "pairs_delivered": len(self._pair_skews_ns),
            "maximum_pair_skew_us": (
                max(self._pair_skews_ns, default=0) / 1_000.0
            ),
        }

    def close(self) -> None:
        super().close()
        for region in getattr(self, "_regions", {}).values():
            region.close()

    def _next_pair(self) -> tuple[SharedRawSnapshot, SharedRawSnapshot]:
        candidates: dict[str, SharedRawSnapshot | None] = {
            "CamL": None,
            "CamR": None,
        }
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            self._producer_check()
            for name in ("CamL", "CamR"):
                if candidates[name] is None:
                    candidate = self._regions[name].latest_after(
                        self._candidate_sequences[name]
                    )
                    if candidate is not None:
                        candidates[name] = candidate
            left = candidates["CamL"]
            right = candidates["CamR"]
            if left is None or right is None:
                time.sleep(0.0005)
                continue
            delta = right.timestamp_ns - left.timestamp_ns
            if abs(delta) <= self._pair_threshold_ns:
                self._candidate_sequences = {
                    "CamL": left.sequence,
                    "CamR": right.sequence,
                }
                return left, right
            older = "CamR" if delta < 0 else "CamL"
            stale = candidates[older]
            assert stale is not None
            self._candidate_sequences[older] = stale.sequence
            self._unmatched[older] += 1
            candidates[older] = None
        raise SourceError("no synchronized live camera pair arrived within 3 seconds")

    def _record_gaps(
        self, left: SharedRawSnapshot, right: SharedRawSnapshot
    ) -> None:
        for name, snapshot in (("CamL", left), ("CamR", right)):
            previous = self._last_delivered_sequences[name]
            if previous is not None and snapshot.sequence > previous + 1:
                self._sequence_drops[name] += snapshot.sequence - previous - 1
            self._last_delivered_sequences[name] = snapshot.sequence

    def _mosaic(self, snapshot: SharedRawSnapshot, camera: str) -> np.ndarray:
        if camera == "CamL":
            height, width, stride = self._height, self._width, self._stride
        else:
            height, width, stride = (
                self._right_height,
                self._right_width,
                self._right_stride,
            )
        words = np.ndarray(
            (height, stride // 2), dtype="<u2", buffer=snapshot.payload
        )
        return words[:, :width]


def resolve_live_calibration_pack(path: Path | None = None) -> Path:
    if path is not None:
        return path.expanduser().resolve()
    fastcap_source = Path(__file__).resolve().parents[3] / "BeanoFastCap/src"
    if str(fastcap_source) not in sys.path:
        sys.path.insert(0, str(fastcap_source))
    try:
        from beanofastcap.calibration_pack import latest_calibration_pack
    except ImportError as exc:
        raise SourceError("cannot import BeanoFastCap calibration discovery") from exc
    return latest_calibration_pack()


def _capture_geometry(
    profile: dict[str, Any], camera: str
) -> tuple[int, int, int, int, float, float]:
    capture = profile["capture"]
    calibration = profile["calibration"]
    width = int(capture["width"])
    height = int(capture["height"])
    stride = int(capture["bytes_per_line"])
    bit_shift = int(capture.get("bit_shift", 0))
    white_level = float(capture["decoded_white_level"])
    dark_level = float(calibration.get("dark_level_median", 0.0))
    if (
        width <= 0
        or height <= 0
        or width % 2
        or height % 2
        or stride < width * 2
        or stride % 2
        or capture.get("cfa") != "RGGB"
        or bit_shift < 0
        or bit_shift > 15
        or white_level <= dark_level
    ):
        raise SourceError(f"{camera} live RAW profile is invalid")
    return width, height, stride, bit_shift, white_level, dark_level


def _signal_lut(white_level: float, dark_level: float) -> np.ndarray:
    levels = np.arange(round(white_level) + 1, dtype=np.float32)
    linear = np.clip(
        (levels - dark_level) / max(white_level - dark_level, 1.0), 0.0, 1.0
    )
    srgb = np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    )
    return np.clip(srgb * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return value
