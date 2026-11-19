import json
import mmap
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import numpy as np

from beanoflight.live_source import (
    FastCapLiveProducer,
    SharedMemoryRawStereoSource,
    SharedRawRegion,
    SourceError,
)

HEADER = struct.Struct("<8s6I4Q")


class PreviewWriter:
    def __init__(self, path: Path, width: int = 8, height: int = 6) -> None:
        self.path = path
        self.width = width
        self.height = height
        self.stride = width * 2
        self.frame_bytes = height * self.stride
        path.write_bytes(b"\0" * (64 + self.frame_bytes))
        self.stream = path.open("r+b")
        self.mapping = mmap.mmap(self.stream.fileno(), 0)
        HEADER.pack_into(
            self.mapping,
            0,
            b"BFCAP01",
            1,
            64,
            width,
            height,
            self.stride,
            self.frame_bytes,
            0,
            0,
            0,
            0,
        )

    def publish(self, sequence: int, timestamp_ns: int, value: int) -> None:
        generation = struct.unpack_from("<Q", self.mapping, 32)[0]
        struct.pack_into("<Q", self.mapping, 32, generation + 1)
        payload = np.full(
            (self.height, self.stride // 2), value, dtype="<u2"
        ).tobytes()
        self.mapping[64:] = payload
        struct.pack_into("<QQQ", self.mapping, 40, sequence, timestamp_ns, len(payload))
        struct.pack_into("<Q", self.mapping, 32, generation + 2)

    def close(self) -> None:
        self.mapping.close()
        self.stream.close()


class LiveSourceTests(unittest.TestCase):
    def test_producer_passes_requested_rate_and_preserves_hardware_anchor(self):
        for rate in (None, 60.0):
            with self.subTest(rate=rate), tempfile.TemporaryDirectory() as directory:
                producer = FastCapLiveProducer(
                    Path('/pack'), Path(directory),
                    {'CamL': Path('/left'), 'CamR': Path('/right')},
                    duration_seconds=30, frame_rate_hz=rate,
                )
                producer._events.put({
                    'event': 'synchronized', 'source_timestamp_ns': 121000000000,
                    'anchor_monotonic_ns': 100000000000,
                    'clock_mapping': 'tegra-cntvct-to-monotonic',
                })
                process = Mock()
                process.poll.return_value = None
                with patch('beanoflight.live_source.subprocess.Popen', return_value=process) as popen, \
                        patch('beanoflight.live_source.threading.Thread'):
                    producer.start()
                command = popen.call_args.args[0]
                if rate is None:
                    self.assertNotIn('--fps', command)
                else:
                    self.assertEqual(command[command.index('--fps') + 1], '60.0')
                self.assertEqual(producer.clock_monotonic_ns, 100000000000)
                self.assertEqual(producer.clock_source_timestamp_ns, 121000000000)
                self.assertEqual(producer.clock_mapping, 'tegra-cntvct-to-monotonic')

    def test_producer_prepares_output_directory_before_launch_in_both_capture_modes(self):
        # Stereo crops and numerical-only jobs share the non-RAW capture path.
        for record_raw in (False, True):
            for existing in (False, True):
                with self.subTest(record_raw=record_raw, existing=existing), \
                        tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / 'new-job' / 'raw'
                    if existing:
                        output.mkdir(parents=True)
                        (output / 'keep.txt').write_text('existing capture data')
                    producer = FastCapLiveProducer(
                        Path('/pack'), output,
                        {'CamL': Path('/left'), 'CamR': Path('/right')},
                        duration_seconds=30, record_raw=record_raw,
                    )
                    producer._events.put({
                        'event': 'synchronized', 'source_timestamp_ns': 121000000000,
                        'anchor_monotonic_ns': 100000000000,
                    })
                    process = Mock()
                    process.poll.return_value = None

                    def launch(command, **kwargs):
                        child_output = Path(command[command.index('--output') + 1])
                        self.assertEqual(child_output, output)
                        self.assertTrue(child_output.is_dir(),
                                        'FastCap requires an existing output root at startup')
                        return process

                    with patch('beanoflight.live_source.subprocess.Popen', side_effect=launch), \
                            patch('beanoflight.live_source.threading.Thread'):
                        producer.start()
                    if existing:
                        self.assertEqual((output / 'keep.txt').read_text(), 'existing capture data')

    def test_producer_rejects_output_file_without_launching_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'raw'
            output.write_text('keep this file')
            producer = FastCapLiveProducer(
                Path('/pack'), output,
                {'CamL': Path('/left'), 'CamR': Path('/right')},
                duration_seconds=30,
            )
            with patch('beanoflight.live_source.subprocess.Popen') as popen:
                with self.assertRaisesRegex(SourceError, 'FastCap output directory'):
                    producer.start(timeout_seconds=0)
                popen.assert_not_called()
            self.assertEqual(output.read_text(), 'keep this file')

    def test_shared_region_reads_only_new_complete_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = PreviewWriter(Path(directory) / "CamL.shm")
            try:
                region = SharedRawRegion(writer.path)
                self.assertIsNone(region.latest_after(None))
                writer.publish(7, 123_000, 321)
                snapshot = region.latest_after(None)
                self.assertEqual(snapshot.sequence, 7)
                self.assertEqual(snapshot.timestamp_ns, 123_000)
                self.assertEqual(len(snapshot.payload), writer.frame_bytes)
                self.assertIsNone(region.latest_after(7))
                region.close()
            finally:
                writer.close()

    def test_live_source_builds_background_and_delivers_sequential_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack = _write_pack(root / "pack")
            left = PreviewWriter(root / "CamL.shm")
            right = PreviewWriter(root / "CamR.shm")
            stop = threading.Event()

            def publish() -> None:
                sequence = 0
                while not stop.is_set():
                    timestamp = 1_000_000_000 + sequence * 16_666_667
                    left.publish(sequence, timestamp, 200 + sequence)
                    right.publish(sequence, timestamp + 1_000, 200 + sequence)
                    sequence += 1
                    stop.wait(0.003)

            thread = threading.Thread(target=publish)
            thread.start()
            source = None
            try:
                source = SharedMemoryRawStereoSource(
                    pack,
                    {"CamL": left.path, "CamR": right.path},
                    frame_count=3,
                    fps=60,
                    clock_source_timestamp_ns=1_000_000_000,
                    clock_monotonic_ns=time.monotonic_ns(),
                )
                background = source.acquire_background(3)
                self.assertEqual(background.shape, (3, 4))
                source.prime()
                first = source.frame(0)
                second = source.frame(1)
                self.assertEqual(first.detection_gray.shape, (3, 4))
                self.assertEqual(first.right_timestamp_ns - source.timestamp_ns(0), 1_000)
                self.assertGreater(source.timestamp_ns(1), source.timestamp_ns(0))
                source.release_frame(first)
                source.release_frame(second)
                statistics = source.stereo_statistics()
                self.assertEqual(statistics["pairs_delivered"], 2)
                self.assertEqual(statistics["maximum_pair_skew_us"], 1.0)
            finally:
                if source is not None:
                    source.close()
                stop.set()
                thread.join()
                left.close()
                right.close()


def _write_pack(root: Path) -> Path:
    (root / "CamL").mkdir(parents=True)
    (root / "CamR").mkdir()
    (root / "geometry").mkdir()
    profile = {
        "capture": {
            "width": 8,
            "height": 6,
            "bytes_per_line": 16,
            "bit_shift": 0,
            "decoded_white_level": 1023,
            "cfa": "RGGB",
        },
        "calibration": {
            "dark_level_median": 0,
            "camera_matrix": [[10, 0, 3.5], [0, 10, 2.5], [0, 0, 1]],
            "distortion_coefficients": [0, 0, 0, 0, 0],
            "wb_enabled": False,
            "color_matrix_enabled": False,
        },
    }
    for camera in ("CamL", "CamR"):
        (root / camera / "profile.json").write_text(json.dumps(profile))
    homography = {
        "schema": "pinkplane-homography/v2",
        "mapping": {
            "direction": "CamL pixels to CamR pixels",
            "coordinate_domain": "undistorted",
            "matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        },
    }
    (root / "geometry/homography.json").write_text(json.dumps(homography))
    return root


if __name__ == "__main__":
    unittest.main()
