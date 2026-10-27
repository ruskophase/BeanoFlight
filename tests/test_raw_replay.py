import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import lz4.block
import numpy as np

from beanoflight.detection import DetectorSettings, RawGreenDetector
from beanoflight.source import (
    MMapRawVideoSource,
    SourceError,
    _raw_green_plane,
    _raw_single_green_plane,
    _stored_value_lut,
    find_raw_bundle,
)


class RawReplayTests(unittest.TestCase):
    def test_precomposed_stored_lut_preserves_decoded_green_values(self):
        decoded_lut = np.arange(1_024, dtype=np.uint16)
        stored_lut = _stored_value_lut(decoded_lut, 4)
        mosaic = np.asarray(
            (
                (160, 320, 480, 640),
                (800, 960, 1_120, 1_280),
                (1_440, 1_600, 1_760, 1_920),
                (2_080, 2_240, 2_400, 2_560),
            ),
            dtype=np.uint16,
        )

        single = _raw_single_green_plane(mosaic, stored_lut)
        averaged = _raw_green_plane(mosaic, stored_lut)

        np.testing.assert_array_equal(single, mosaic[0::2, 1::2] >> 4)
        expected_stored = cv2.addWeighted(
            mosaic[0::2, 1::2],
            0.5,
            mosaic[1::2, 0::2],
            0.5,
            0.0,
        )
        np.testing.assert_array_equal(averaged, expected_stored >> 4)

    def test_contour_localizer_recovers_native_sensor_centroid(self):
        source = MMapRawVideoSource.__new__(MMapRawVideoSource)
        source._stereo_refinement_threshold = 22
        source._stereo_max_refinement_px = 64.0
        source._stereo_close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (5, 5)
        )
        source._stereo_open_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (3, 3)
        )
        current = np.zeros((80, 80), dtype=np.uint8)
        cv2.ellipse(current, (30, 25), (10, 8), 0, 0, 360, 180, -1)

        result = source._foreground_component(
            current,
            np.zeros_like(current),
            native_left=0,
            native_top=0,
            native_right=160,
            native_bottom=160,
            projected_px=(60.0, 50.0),
        )

        self.assertIsNotNone(result)
        centroid, area, component_size = result
        self.assertAlmostEqual(centroid[0], 60.0, delta=1.0)
        self.assertAlmostEqual(centroid[1], 50.0, delta=1.0)
        self.assertGreater(area, 600)
        self.assertGreaterEqual(component_size[0], 40)
        self.assertGreaterEqual(component_size[1], 32)

    def test_reverse_stereo_projection_inverts_forward_homography(self):
        from beanoflight.stereo import StereoPointCalibration

        matrix = np.asarray(
            ((1.1, 0.02, 14.0), (-0.01, 0.95, 8.0), (0.0, 0.0, 1.0)),
            dtype=np.float64,
        )
        identity = np.eye(3, dtype=np.float64)
        calibration = StereoPointCalibration(
            Path("homography.json"),
            matrix,
            np.linalg.inv(matrix),
            identity,
            np.zeros(5),
            identity,
            np.zeros(5),
        )
        point = (100.0, 75.0)
        right = calibration.project_distorted_caml_to_distorted_camr(point)
        recovered = calibration.project_distorted_camr_to_undistorted_caml(right)

        self.assertAlmostEqual(recovered[0], point[0], places=6)
        self.assertAlmostEqual(recovered[1], point[1], places=6)

    def test_mmaps_green_plane_and_defers_crop_colour_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_bundle(root)
            source = MMapRawVideoSource(root)

            frame = source.frame(1)
            self.assertEqual(frame.detection_gray.shape, (4, 4))
            self.assertEqual(frame.detection_gray.dtype, np.uint8)
            prepared = source.prepare_crop(frame, (4.0, 4.0), 4, allow_padding=False)
            self.assertIsNotNone(prepared)
            materialize, width, height, padded = prepared
            self.assertEqual((width, height, padded), (4, 4, False))
            source.release_frame(frame)
            with self.assertRaises(SourceError):
                _unused = frame.mosaic

            crop = materialize()
            self.assertEqual(crop.shape, (4, 4, 3))
            self.assertEqual(crop.dtype, np.uint8)
            self.assertGreater(float(crop.mean()), 0.0)
            self.assertEqual(source.undistort_point((3.0, 2.0)), (3.0, 2.0))
            self.assertEqual(
                source.undistort_points(((3.0, 2.0), (4.0, 5.0))),
                ((3.0, 2.0), (4.0, 5.0)),
            )
            source.close()

    def test_mmaps_offset_frames_from_contiguous_raw_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_bundle(root)
            metadata = root / "metadata/CamL.csv"
            with metadata.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            payloads = [(root / row["raw_path"]).read_bytes() for row in rows]
            stream_path = root / "raw/CamL/frames.raw"
            stream_path.write_bytes(b"".join(payloads))
            fields = ("frame_index", "timestamp_ns", "raw_path", "raw_offset")
            with metadata.open("w", newline="", encoding="utf-8") as stream:
                output = csv.DictWriter(stream, fieldnames=fields)
                output.writeheader()
                offset = 0
                for row, payload in zip(rows, payloads, strict=True):
                    output.writerow(
                        {
                            "frame_index": row["frame_index"],
                            "timestamp_ns": row["timestamp_ns"],
                            "raw_path": "raw/CamL/frames.raw",
                            "raw_offset": offset,
                        }
                    )
                    offset += len(payload)

            source = MMapRawVideoSource(root)
            first = source.frame(0)
            second = source.frame(1)
            self.assertLess(
                float(first.detection_gray.mean()),
                float(second.detection_gray.mean()),
            )
            self.assertEqual(first.path, stream_path.resolve())
            self.assertEqual(second.path, stream_path.resolve())
            source.release_frame(first)
            source.release_frame(second)
            source.close()

    def test_replays_independently_compressed_lz4_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_bundle(root)
            metadata = root / "metadata/CamL.csv"
            with metadata.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            payloads = [(root / row["raw_path"]).read_bytes() for row in rows]
            stream_path = root / "raw/CamL/frames.lz4"
            fields = (
                "frame_index",
                "timestamp_ns",
                "bytes_used",
                "raw_path",
                "raw_offset",
                "stored_bytes",
                "stored_span_bytes",
                "raw_codec",
            )
            offset = 0
            output_rows = []
            with stream_path.open("wb") as encoded:
                for row, payload in zip(rows, payloads, strict=True):
                    compressed = lz4.block.compress(payload, store_size=False)
                    span = ((len(compressed) + 4095) // 4096) * 4096
                    encoded.write(compressed)
                    encoded.write(bytes(span - len(compressed)))
                    output_rows.append(
                        {
                            "frame_index": row["frame_index"],
                            "timestamp_ns": row["timestamp_ns"],
                            "bytes_used": len(payload),
                            "raw_path": "raw/CamL/frames.lz4",
                            "raw_offset": offset,
                            "stored_bytes": len(compressed),
                            "stored_span_bytes": span,
                            "raw_codec": "lz4-block",
                        }
                    )
                    offset += span
            with metadata.open("w", newline="", encoding="utf-8") as stream:
                output = csv.DictWriter(stream, fieldnames=fields)
                output.writeheader()
                output.writerows(output_rows)

            source = MMapRawVideoSource(root)
            first = source.frame(0)
            second = source.frame(1)
            self.assertLess(float(first.detection_gray.mean()), float(second.detection_gray.mean()))
            self.assertEqual(first.path, stream_path.resolve())
            self.assertIsNone(first._mapping)
            self.assertIsNotNone(first._payload)
            source.release_frame(first)
            source.release_frame(second)
            source.close()

    def test_ml_fast_crop_is_linear_and_calibrated_reference_remains_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_bundle(root)
            fast = MMapRawVideoSource(root, crop_processing="ml-fast")
            calibrated = MMapRawVideoSource(root, crop_processing="calibrated")
            fast_frame = fast.frame(1)
            calibrated_frame = calibrated.frame(1)

            fast_crop, fast_padded = fast.extract_crop(
                fast_frame, (4.0, 4.0), 4, allow_padding=False
            )
            calibrated_crop, calibrated_padded = calibrated.extract_crop(
                calibrated_frame, (4.0, 4.0), 4, allow_padding=False
            )

            self.assertFalse(fast_padded)
            self.assertFalse(calibrated_padded)
            self.assertEqual(fast.crop_processing_profile, "ml-fast")
            self.assertEqual(calibrated.crop_processing_profile, "calibrated")
            self.assertLess(float(fast_crop.mean()), float(calibrated_crop.mean()))
            self.assertGreater(
                float(fast_crop[..., 2].mean()), float(fast_crop[..., 1].mean())
            )
            self.assertGreater(
                float(fast_crop[..., 1].mean()), float(fast_crop[..., 0].mean())
            )
            fast.release_frame(fast_frame)
            calibrated.release_frame(calibrated_frame)
            fast.close()
            calibrated.close()

    def test_rejects_unknown_crop_processing_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_bundle(root)

            with self.assertRaisesRegex(SourceError, "RAW crop processing"):
                MMapRawVideoSource(root, crop_processing="unknown")

    def test_resolves_bundle_from_calibrated_derivative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_bundle(root)
            derivative = root / "postprocess/CamL-calibrated.mkv"
            derivative.parent.mkdir()
            derivative.touch()
            self.assertEqual(find_raw_bundle(derivative), root.resolve())

    def test_green_detector_reports_native_coordinates(self):
        background = np.zeros((30, 40), dtype=np.uint8)
        gray = background.copy()
        cv2.ellipse(gray, (20, 8), (6, 4), 0, 0, 360, 180, -1)
        frame = SimpleNamespace(
            detection_gray=gray,
            native_size_px=(80, 60),
        )
        detector = RawGreenDetector(
            DetectorSettings(
                processing_scale=0.5,
                blur_kernel=1,
                threshold=10,
                close_kernel=1,
                open_kernel=1,
                min_area_px=100,
                max_area_px=2_000,
                min_width_px=8,
                max_width_px=40,
                min_height_px=8,
                max_height_px=40,
            )
        )
        result = detector.detect(frame, background)
        self.assertEqual(len(result.detections), 1)
        x, y = result.detections[0].centroid_px
        self.assertAlmostEqual(x, 40.0, delta=1.0)
        self.assertAlmostEqual(y, 16.0, delta=1.0)

    @staticmethod
    def _write_bundle(root: Path) -> None:
        (root / "metadata").mkdir(parents=True)
        (root / "raw/CamL").mkdir(parents=True)
        artifacts = root / "calibration/CamL/artifacts/test"
        artifacts.mkdir(parents=True)
        np.save(artifacts / "master_dark.npy", np.zeros((8, 8), np.float32))
        np.save(artifacts / "flat_gain.npy", np.ones((8, 8), np.float32))
        np.save(artifacts / "defect_map.npy", np.zeros((8, 8), bool))
        profile = {
            "capture": {
                "width": 8,
                "height": 8,
                "bytes_per_line": 20,
                "bit_shift": 4,
                "cfa": "RGGB",
                "decoded_white_level": 1023.0,
            },
            "processing": {"demosaic": "bilinear"},
            "artifacts": {
                name: {"path": f"artifacts/test/{name}.npy"}
                for name in ("master_dark", "flat_gain", "defect_map")
            },
            "calibration": {
                "dark_level_median": 0.0,
                "wb_enabled": False,
                "color_matrix_enabled": False,
                "camera_matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "distortion_coefficients": [0.0, 0.0, 0.0, 0.0, 0.0],
            },
        }
        (root / "calibration/CamL/profile.json").write_text(
            json.dumps(profile), encoding="utf-8"
        )
        (root / "recording.json").write_text(
            json.dumps({"plan": {"frame_rate_hz": 60.0}}), encoding="utf-8"
        )
        fields = ("frame_index", "timestamp_ns", "raw_path")
        with (root / "metadata/CamL.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            output = csv.DictWriter(stream, fieldnames=fields)
            output.writeheader()
            for index, value in enumerate((100, 500)):
                relative = Path(f"raw/CamL/frame-{index}.raw")
                words = np.zeros((8, 10), dtype="<u2")
                words[0::2, 0:8:2] = value << 4
                words[0::2, 1:8:2] = (value * 3 // 5) << 4
                words[1::2, 0:8:2] = (value * 3 // 5) << 4
                words[1::2, 1:8:2] = (value // 5) << 4
                (root / relative).write_bytes(words.tobytes())
                output.writerow(
                    {
                        "frame_index": index,
                        "timestamp_ns": 1_000 + index * 10,
                        "raw_path": relative,
                    }
                )


if __name__ == "__main__":
    unittest.main()
