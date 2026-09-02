import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from beanoflight.calibration import MetricPlaneCalibration
from beanoflight.crop import CropPayload
from beanoflight.detection import DetectorSettings, RawGreenDetector
from beanoflight.live_statistics import (
    LiveStatisticsCollector,
    LiveStatisticsSettings,
)
from beanoflight.models import (
    BeanRef,
    Detection,
    FrameAnalysis,
    Observation,
    TrackSnapshot,
    TrackStatus,
)
from beanoflight.registry_models import InferenceJob, InferenceStatus
from beanoflight.source import RawReplayFrame
from beanoflight.stereo import StereoCropPreparation, StereoPairMetadata


class _IdentityStereo:
    @staticmethod
    def project_distorted_camr_to_undistorted_caml(point):
        return point


class _StatisticsSource:
    def __init__(self):
        self.stereo_calibration = _IdentityStereo()
        self.crop_processing_profile = "ml-fast"
        self.metadata = SimpleNamespace(width=160, height=160)
        self.background = np.zeros((80, 80), dtype=np.uint8)
        self.current = self.background.copy()
        cv2.ellipse(self.current, (40, 40), (14, 10), 0, 0, 360, 180, -1)
        self.image = np.zeros((160, 160, 3), dtype=np.uint8)
        cv2.ellipse(self.image, (80, 80), (28, 20), 0, 0, 360, (45, 120, 205), -1)
        self.mask = np.zeros((160, 160), dtype=np.uint8)
        cv2.ellipse(self.mask, (80, 80), (28, 20), 0, 0, 360, 255, -1)

    @staticmethod
    def configure_statistics_processing():
        return None

    def right_background_gray(self):
        return self.background

    def right_detection_gray(self, _frame):
        return self.current.copy()

    @staticmethod
    def undistort_point(point):
        return point

    def prepare_statistics_stereo_crop(
        self,
        _frame,
        centroid,
        size,
        *,
        allow_padding,
        allow_resize,
    ):
        self.assertions = (allow_padding, allow_resize)
        pair = StereoPairMetadata(
            left_frame_index=0,
            right_frame_index=10,
            left_timestamp_ns=1_000,
            right_timestamp_ns=1_200,
            caml_centroid_px=centroid,
            camr_projected_centroid_px=centroid,
            camr_centroid_px=centroid,
            refinement_distance_px=0.0,
            refinement_area_px=2_000,
        )
        return StereoCropPreparation(
            lambda: self.image.copy(),
            lambda: self.image.copy(),
            size,
            size,
            size,
            False,
            pair,
            self.mask.copy(),
        )

    def inference_statistics_camr_component(self, _frame, _pair):
        return self.mask.copy(), (0, 0)

    def prepare_crop(self, _frame, _centroid, size, *, allow_padding):
        self.fallback_assertions = (size, allow_padding)
        return lambda: self.image.copy(), size, size, False


def _calibration() -> MetricPlaneCalibration:
    identity = np.eye(3, dtype=np.float64)
    return MetricPlaneCalibration(
        Path("homography.json"),
        "0" * 64,
        (160, 160),
        9.16,
        identity,
        identity,
        0.0,
        0.0,
        0.0,
        160.0,
    )


def _analysis(
    ref: BeanRef,
    frame_index: int,
    y_mm: float,
    *,
    status: TrackStatus = TrackStatus.CONFIRMED,
) -> FrameAnalysis:
    detection = Detection((80.0, 80.0), (50, 60, 60, 40), 2_000, 0.9, (80, 80, 80))
    observation = Observation(
        frame_index,
        1_000 + frame_index * 1_000,
        detection,
        (0.0, y_mm),
    )
    track = TrackSnapshot(
        ref,
        status,
        observation.timestamp_ns,
        (0.0, y_mm, 0.0, 100.0),
        tuple(tuple(float(row == column) for column in range(4)) for row in range(4)),
        frame_index + 2,
        0,
        detection.bbox_px,
        (observation,),
    )
    return FrameAnalysis(
        frame_index,
        observation.timestamp_ns,
        (detection,),
        (),
        (track,),
        (),
        1.0,
    )


class LiveStatisticsTests(unittest.TestCase):
    def test_settings_hard_cap_live_capture_at_two_samples(self):
        with self.assertRaises(ValueError):
            LiveStatisticsSettings(target_samples_per_bean=3).validate()

    def test_detector_reuses_current_component_for_native_crop_mask(self):
        current = np.zeros((80, 80), dtype=np.uint8)
        background = current.copy()
        cv2.ellipse(current, (40, 40), (12, 9), 0, 0, 360, 180, -1)
        mosaic = np.zeros((160, 160), dtype=np.uint16)
        frame = RawReplayFrame(
            0,
            Path("frame.raw"),
            current,
            (160, 160),
            None,
            mosaic,
        )
        detector = RawGreenDetector(
            DetectorSettings(
                blur_kernel=1,
                threshold=10,
                close_kernel=1,
                open_kernel=1,
                min_area_px=100,
                max_area_px=20_000,
                min_width_px=8,
                max_width_px=150,
                min_height_px=8,
                max_height_px=150,
            )
        )
        result = detector.detect(frame, background)
        self.assertEqual(len(result.detections), 1)

        mask = detector.component_crop_mask(
            result.detections[0],
            result.detections[0].centroid_px,
            120,
        )

        self.assertIsNotNone(mask)
        self.assertEqual(mask.shape, (120, 120))
        self.assertGreater(cv2.countNonZero(mask), 1_000)

    def test_inference_attached_collector_reuses_materialized_stereo_crop(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _StatisticsSource()
            detector = RawGreenDetector()
            detector.component_mask_evidence = (
                lambda *_args, **_kwargs: (source.mask.copy(), (0, 0))
            )
            output = Path(temporary) / "capture"
            collector = LiveStatisticsCollector(
                source,
                detector,
                source.background,
                _calibration(),
                output,
                settings=LiveStatisticsSettings(flush_every=1),
            )
            ref = BeanRef("d" * 32, 4)
            analysis = _analysis(ref, 0, 40.0)
            pair = StereoPairMetadata(
                left_frame_index=0,
                right_frame_index=10,
                left_timestamp_ns=1_000,
                right_timestamp_ns=1_200,
                caml_centroid_px=(80.0, 80.0),
                camr_projected_centroid_px=(80.0, 80.0),
                camr_centroid_px=(80.0, 80.0),
                refinement_distance_px=0.0,
                refinement_area_px=2_100,
            )
            job = InferenceJob(
                job_id=f"infer:{ref.run_id}:{ref.sequence}:CamL:0:0",
                bean_ref=ref,
                status=InferenceStatus.SUBMITTED,
                camera_id="CamL",
                frame_index=0,
                capture_timestamp_ns=1_000,
                source_registry_revision=1,
                crop_width_px=160,
                crop_height_px=160,
                padded=False,
                submitted_timestamp_ns=1_000,
                updated_timestamp_ns=1_000,
                source_crop_width_px=160,
                source_crop_height_px=160,
            )
            payload = CropPayload(
                job,
                source.image.copy(),
                None,
                source.image.copy(),
                None,
                pair,
            )
            collector.start(ref.run_id)
            attached = collector.attach_to_inference(
                SimpleNamespace(index=0),
                analysis,
                (payload,),
            )
            collector.ingest_materialized(attached)
            self._wait_for_samples(collector, 1)
            metrics = collector.close()

            observation = json.loads(
                (output / "observations.jsonl").read_text().strip()
            )
            self.assertEqual(metrics["beans_without_samples"], 0)
            self.assertEqual(metrics["beans_with_one_sample"], 1)
            self.assertEqual(
                observation["schema"],
                "beanoflight-live-statistics-observation/v2",
            )
            self.assertEqual(observation["capture_path"], "inference-attached")
            self.assertEqual(observation["measurement_view_count"], 2)
            self.assertTrue(observation["camr_measurement_available"])
            self.assertEqual(observation["caml_detection_area_px"], 2_000)
            self.assertEqual(observation["camr_refinement_area_px"], 2_100)
            self.assertIn("caml_b_sum", observation)
            self.assertIn("camr_mask_variance_x_px2", observation)
            self.assertNotIn("caml_lab_l_mean", observation)

    def test_inference_attached_collector_recovers_zero_sample_with_caml(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _StatisticsSource()
            detector = RawGreenDetector()
            detector.component_mask_evidence = (
                lambda *_args, **_kwargs: (source.mask.copy(), (0, 0))
            )
            output = Path(temporary) / "capture"
            collector = LiveStatisticsCollector(
                source,
                detector,
                source.background,
                _calibration(),
                output,
                settings=LiveStatisticsSettings(flush_every=1),
            )
            ref = BeanRef("e" * 32, 5)
            analysis = _analysis(ref, 0, 40.0)
            collector.start(ref.run_id)
            collector.attach_to_inference(
                SimpleNamespace(index=0),
                analysis,
                (),
            )
            collector.cache_unattached_primary(
                SimpleNamespace(index=0),
                analysis,
                (),
            )
            metrics = collector.close()

            observation = json.loads(
                (output / "observations.jsonl").read_text().strip()
            )
            self.assertEqual(metrics["beans_without_samples"], 0)
            self.assertEqual(metrics["beans_with_one_sample"], 1)
            self.assertEqual(
                observation["capture_path"],
                "caml-only-zero-sample-fallback",
            )
            self.assertEqual(observation["measurement_view_count"], 1)
            self.assertFalse(observation["camr_measurement_available"])
            self.assertIn("caml_b_sum", observation)
            self.assertNotIn("camr_b_sum", observation)
            capture = json.loads((output / "capture.json").read_text())
            self.assertEqual(
                capture["schema"],
                "beanoflight-live-statistics-capture/v2",
            )

    def test_caml_fallback_upgrades_when_later_frame_has_complete_crop(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _StatisticsSource()
            source.metadata = SimpleNamespace(width=160, height=160)

            def prepare(frame, _centroid, size, *, allow_padding):
                self.assertFalse(allow_padding)
                if frame.index == 0:
                    return None
                return lambda: source.image.copy(), size, size, False

            source.prepare_crop = prepare
            detector = RawGreenDetector()
            detector.component_mask_evidence = (
                lambda *_args, **_kwargs: (source.mask.copy(), (0, 0))
            )
            output = Path(temporary) / "capture"
            collector = LiveStatisticsCollector(
                source,
                detector,
                source.background,
                _calibration(),
                output,
                settings=LiveStatisticsSettings(flush_every=1),
            )
            ref = BeanRef("f" * 32, 6)
            collector.start(ref.run_id)
            for frame_index in (0, 1):
                analysis = _analysis(ref, frame_index, 40.0)
                collector.attach_to_inference(
                    SimpleNamespace(index=frame_index),
                    analysis,
                    (),
                )
                collector.cache_unattached_primary(
                    SimpleNamespace(index=frame_index),
                    analysis,
                    (),
                )
            metrics = collector.close()

            observation = json.loads(
                (output / "observations.jsonl").read_text().strip()
            )
            self.assertTrue(observation["feature_enrichment_valid"])
            self.assertEqual(
                metrics["counts"]["caml_fallback_candidates_upgraded"],
                1,
            )

    def test_collector_persists_two_numerical_sets_and_no_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _StatisticsSource()
            detector = RawGreenDetector()
            component = np.zeros((160, 160), dtype=np.uint8)
            cv2.ellipse(component, (80, 80), (28, 20), 0, 0, 360, 255, -1)
            detector.component_crop_mask = lambda *_args, **_kwargs: component.copy()
            output = Path(temporary) / "capture"
            collector = LiveStatisticsCollector(
                source,
                detector,
                source.background,
                _calibration(),
                output,
                settings=LiveStatisticsSettings(
                    inference_attached=False,
                    crop_size_px=160,
                    queue_capacity=4,
                    primary_queue_reserve=2,
                    minimum_sample_frame_gap=2,
                    flush_every=1,
                ),
            )
            ref = BeanRef("a" * 32, 1)
            collector.start(ref.run_id)
            collector.consider(SimpleNamespace(), _analysis(ref, 0, 20.0))
            self._wait_for_samples(collector, 1)
            collector.consider(SimpleNamespace(), _analysis(ref, 1, 80.0))
            collector.consider(SimpleNamespace(), _analysis(ref, 3, 80.0))
            self._wait_for_samples(collector, 2)
            collector.consider(SimpleNamespace(), _analysis(ref, 6, 140.0))
            metrics = collector.close()

            observations = [
                json.loads(line)
                for line in (output / "observations.jsonl").read_text().splitlines()
            ]
            ledger = [
                json.loads(line)
                for line in (output / "beans.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(observations), 2)
            self.assertEqual([row["sample_index"] for row in observations], [1, 2])
            self.assertEqual({row["fov_band"] for row in observations}, {"top", "middle"})
            self.assertEqual(metrics["beans_with_two_samples"], 1)
            self.assertEqual(ledger[0]["sample_count"], 2)
            self.assertFalse(ledger[0]["single_sample_fallback"])
            self.assertFalse(
                any(
                    path.suffix.lower() in {".png", ".jpg", ".mkv", ".raw"}
                    for path in output.rglob("*")
                )
            )
            capture = json.loads((output / "capture.json").read_text())
            self.assertEqual(capture["status"], "completed")
            self.assertEqual(
                capture["settings"]["hard_maximum_samples_per_bean"], 2
            )
            self.assertIn("caml_lab_l_mean", observations[0])
            self.assertIn("rotational_ellipsoid_volume_proxy_mm3", observations[0])

    def test_collector_records_single_sample_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _StatisticsSource()
            detector = RawGreenDetector()
            detector.component_crop_mask = lambda *_args, **_kwargs: source.mask.copy()
            output = Path(temporary) / "capture"
            collector = LiveStatisticsCollector(
                source,
                detector,
                source.background,
                _calibration(),
                output,
                settings=LiveStatisticsSettings(
                    inference_attached=False,
                    flush_every=1,
                ),
            )
            ref = BeanRef("b" * 32, 2)
            collector.start(ref.run_id)
            collector.consider(SimpleNamespace(), _analysis(ref, 0, 40.0))
            self._wait_for_samples(collector, 1)
            metrics = collector.close()

            ledger = json.loads((output / "beans.jsonl").read_text().strip())
            self.assertEqual(metrics["beans_with_one_sample"], 1)
            self.assertEqual(ledger["sample_count"], 1)
            self.assertTrue(ledger["single_sample_fallback"])

    def test_tentative_observation_is_excluded_from_confirmed_ledger_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = _StatisticsSource()
            detector = RawGreenDetector()
            detector.component_crop_mask = lambda *_args, **_kwargs: source.mask.copy()
            output = Path(temporary) / "capture"
            collector = LiveStatisticsCollector(
                source,
                detector,
                source.background,
                _calibration(),
                output,
                settings=LiveStatisticsSettings(
                    inference_attached=False,
                    flush_every=1,
                ),
            )
            ref = BeanRef("c" * 32, 3)
            collector.start(ref.run_id)
            collector.consider(
                SimpleNamespace(),
                _analysis(ref, 0, 40.0, status=TrackStatus.TENTATIVE),
            )
            self._wait_for_samples(collector, 1, confirmed_only=False)
            metrics = collector.close()

            self.assertEqual(metrics["confirmed_beans"], 0)
            self.assertEqual(metrics["observations_persisted"], 0)
            self.assertEqual(metrics["total_observations_persisted"], 1)
            self.assertEqual(metrics["unconfirmed_observations_persisted"], 1)
            self.assertEqual((output / "beans.jsonl").read_text(), "")

    @staticmethod
    def _wait_for_samples(
        collector: LiveStatisticsCollector,
        count: int,
        *,
        confirmed_only: bool = True,
    ) -> None:
        deadline = time.monotonic() + 3.0
        key = (
            "observations_persisted"
            if confirmed_only
            else "total_observations_persisted"
        )
        while time.monotonic() < deadline:
            if collector.statistics()[key] >= count:
                return
            time.sleep(0.01)
        raise AssertionError(f"statistics collector did not persist {count} samples")


if __name__ == "__main__":
    unittest.main()
