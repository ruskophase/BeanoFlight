import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from beanoflight.crop import (
    BeanCropSelector,
    CropPayload,
    CropSettings,
    extract_square_crop,
)
from beanoflight.inference_transport import ZeroMQCropClient, ZeroMQCropReceiver
from beanoflight.models import (
    BeanRef,
    Detection,
    FrameAnalysis,
    Observation,
    TrackSnapshot,
    TrackStatus,
)
from beanoflight.registry_models import InferenceJob, InferenceStatus


class CropTests(unittest.TestCase):
    def test_default_crop_is_model_sized_and_requires_complete_evidence(self):
        settings = CropSettings()

        self.assertEqual(settings.size_px, 224)
        self.assertFalse(settings.allow_padding)
        self.assertTrue(settings.adaptive_edge_resize)

    def test_crops_per_bean_is_limited_to_five(self):
        for count in range(1, 6):
            CropSettings(max_crops_per_bean=count).validate()
        for count in (0, 6):
            with self.assertRaises(ValueError):
                CropSettings(max_crops_per_bean=count).validate()

    def test_selector_sends_requested_number_of_successive_crops(self):
        selector = BeanCropSelector(CropSettings(size_px=20, max_crops_per_bean=3))
        bean_ref = BeanRef("crop-run", 1)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        observations = []
        selected = []
        for index in range(5):
            observations.append(
                Observation(
                    index,
                    index * 10,
                    Detection((50.0, 50.0), (40, 40, 20, 20), 400, 1.0, (0, 0, 0)),
                    (0.0, float(index)),
                )
            )
            track = TrackSnapshot(
                bean_ref,
                TrackStatus.CONFIRMED,
                index * 10,
                (0.0, float(index), 0.0, 1.0),
                tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
                index + 2,
                0,
                (40, 40, 20, 20),
                tuple(observations),
            )
            analysis = FrameAnalysis(
                index,
                index * 10,
                (),
                (),
                (track,),
                (),
                0.1,
            )
            selected.extend(selector.select(frame, analysis, {bean_ref: index + 1}))
        self.assertEqual(len(selected), 3)
        self.assertEqual([item.job.frame_index for item in selected], [0, 1, 2])
        self.assertEqual(len({item.job.job_id for item in selected}), 3)

    def test_failed_delivery_releases_sample_for_next_complete_frame(self):
        selector = BeanCropSelector(CropSettings(size_px=20, max_crops_per_bean=2))
        bean_ref = BeanRef("retry-run", 1)
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        observations = []

        def select(index):
            observations.append(
                Observation(
                    index,
                    index * 10,
                    Detection(
                        (50.0, 50.0),
                        (40, 40, 20, 20),
                        400,
                        1.0,
                        (0, 0, 0),
                    ),
                    (0.0, float(index)),
                )
            )
            track = TrackSnapshot(
                bean_ref,
                TrackStatus.CONFIRMED,
                index * 10,
                (0.0, float(index), 0.0, 1.0),
                tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
                index + 2,
                0,
                (40, 40, 20, 20),
                tuple(observations),
            )
            return selector.select(
                frame,
                FrameAnalysis(index, index * 10, (), (), (track,), (), 0.1),
                {bean_ref: index + 1},
            )

        first = select(0)
        selector.delivery_failed(first)
        replacement = select(1)
        selector.delivery_succeeded(replacement)
        second = select(2)
        selector.delivery_succeeded(second)

        self.assertEqual(first[0].job.job_id.rsplit(":", 1)[1], "0")
        self.assertEqual(replacement[0].job.job_id.rsplit(":", 1)[1], "0")
        self.assertEqual(second[0].job.job_id.rsplit(":", 1)[1], "1")
        self.assertEqual(select(3), ())

    def test_selector_defers_clipped_tentative_crop_until_next_observation(self):
        selector = BeanCropSelector(CropSettings(size_px=20))
        bean_ref = BeanRef("early-run", 1)
        observation = Observation(
            0,
            100,
            Detection((50.0, 5.0), (40, 0, 20, 10), 200, 1.0, (0, 0, 0)),
            (0.0, -10.0),
        )
        track = TrackSnapshot(
            bean_ref,
            TrackStatus.TENTATIVE,
            100,
            (0.0, -10.0, 0.0, 1.0),
            tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
            1,
            0,
            observation.detection.bbox_px,
            (observation,),
        )
        analysis = FrameAnalysis(0, 100, (), (), (track,), (), 0.1)

        first = selector.select(
            np.zeros((100, 100, 3), dtype=np.uint8),
            analysis,
            {bean_ref: 1},
        )
        second_observation = Observation(
            1,
            110,
            Detection((50.0, 20.0), (40, 10, 20, 20), 400, 1.0, (0, 0, 0)),
            (0.0, -5.0),
        )
        confirmed = TrackSnapshot(
            bean_ref,
            TrackStatus.CONFIRMED,
            110,
            (0.0, -5.0, 0.0, 1.0),
            tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
            2,
            0,
            second_observation.detection.bbox_px,
            (observation, second_observation),
        )
        second = selector.select(
            np.zeros((100, 100, 3), dtype=np.uint8),
            FrameAnalysis(1, 110, (), (), (confirmed,), (), 0.1),
            {bean_ref: 2},
        )

        self.assertEqual(first, ())
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].job.frame_index, 1)
        self.assertFalse(second[0].job.padded)

    def test_selector_resizes_early_crop_when_bean_is_fully_visible(self):
        selector = BeanCropSelector(CropSettings(size_px=40))
        bean_ref = BeanRef("edge-run", 1)
        observation = Observation(
            0,
            100,
            Detection((50.0, 15.0), (42, 5, 16, 20), 250, 1.0, (0, 0, 0)),
            (0.0, -10.0),
        )
        track = TrackSnapshot(
            bean_ref,
            TrackStatus.TENTATIVE,
            100,
            (0.0, -10.0, 0.0, 1.0),
            tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
            1,
            0,
            observation.detection.bbox_px,
            (observation,),
        )

        selected = selector.select(
            np.zeros((100, 100, 3), dtype=np.uint8),
            FrameAnalysis(0, 100, (), (), (track,), (), 0.1),
            {bean_ref: 1},
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].image_bgr.shape, (40, 40, 3))
        self.assertTrue(selected[0].job.resized)
        self.assertEqual(selected[0].job.source_crop_width_px, 30)
        self.assertFalse(selected[0].job.padded)

    def test_selector_defers_edge_crop_when_adaptive_resize_is_disabled(self):
        selector = BeanCropSelector(
            CropSettings(size_px=40, adaptive_edge_resize=False)
        )
        bean_ref = BeanRef("edge-run", 1)
        observation = Observation(
            0,
            100,
            Detection((50.0, 15.0), (42, 5, 16, 20), 250, 1.0, (0, 0, 0)),
            (0.0, -10.0),
        )
        track = TrackSnapshot(
            bean_ref,
            TrackStatus.TENTATIVE,
            100,
            (0.0, -10.0, 0.0, 1.0),
            tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
            1,
            0,
            observation.detection.bbox_px,
            (observation,),
        )

        selected = selector.select(
            np.zeros((100, 100, 3), dtype=np.uint8),
            FrameAnalysis(0, 100, (), (), (track,), (), 0.1),
            {bean_ref: 1},
        )

        self.assertEqual(selected, ())

    def test_extracts_centred_lossless_crop_and_optional_padding(self):
        frame = np.arange(80 * 100 * 3, dtype=np.uint8).reshape(80, 100, 3)
        crop, padded = extract_square_crop(frame, (50.0, 40.0), 20, allow_padding=False)
        self.assertFalse(padded)
        np.testing.assert_array_equal(crop, frame[30:50, 40:60])
        self.assertTrue(crop.flags.c_contiguous)

        missing, _ = extract_square_crop(frame, (2.0, 2.0), 20, allow_padding=False)
        self.assertIsNone(missing)
        padded_crop, padded = extract_square_crop(
            frame, (2.0, 2.0), 20, allow_padding=True
        )
        self.assertTrue(padded)
        self.assertEqual(padded_crop.shape, (20, 20, 3))

    def test_zero_mq_crop_round_trip_is_byte_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'crops.sock'}"
            receiver = ZeroMQCropReceiver(endpoint)
            job = InferenceJob(
                "job-1",
                BeanRef("run", 1),
                InferenceStatus.SUBMITTED,
                "CamL",
                7,
                100,
                3,
                30,
                30,
                False,
                100,
                100,
            )
            image = np.random.default_rng(4).integers(
                0, 256, size=(30, 30, 3), dtype=np.uint8
            )
            observed = []

            def receive():
                observed.append(receiver.receive(timeout_ms=2_000))

            thread = threading.Thread(target=receive)
            thread.start()
            client = ZeroMQCropClient(endpoint, timeout_ms=2_000)
            deferred = CropPayload(job, None, lambda: image)
            self.assertIsNone(deferred.image_bgr)
            client.submit(deferred.materialized())
            thread.join(2.0)
            client.close()
            receiver.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed[0].job, job)
            np.testing.assert_array_equal(observed[0].image_bgr, image)

    def test_zero_mq_frame_batch_round_trip_is_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'batch-crops.sock'}"
            receiver = ZeroMQCropReceiver(endpoint)
            images = tuple(
                np.full((12, 12, 3), sequence, dtype=np.uint8)
                for sequence in (1, 2, 3)
            )
            payloads = tuple(
                CropPayload(
                    InferenceJob(
                        f"job-{sequence}",
                        BeanRef("frame-run", sequence),
                        InferenceStatus.SUBMITTED,
                        "CamL",
                        22,
                        100,
                        1,
                        12,
                        12,
                        False,
                        100,
                        100,
                    ),
                    image,
                )
                for sequence, image in zip((1, 2, 3), images)
            )
            observed = []

            def receive_batch():
                observed.append(receiver.receive_batch(timeout_ms=2_000))

            thread = threading.Thread(target=receive_batch)
            thread.start()
            client = ZeroMQCropClient(endpoint, timeout_ms=2_000)
            client.submit_batch(payloads)
            thread.join(2.0)
            client.close()
            receiver.close()

            self.assertFalse(thread.is_alive())
            self.assertEqual(tuple(item.job for item in observed[0]), tuple(
                item.job for item in payloads
            ))
            for received, expected in zip(observed[0], images):
                np.testing.assert_array_equal(received.image_bgr, expected)


if __name__ == "__main__":
    unittest.main()
