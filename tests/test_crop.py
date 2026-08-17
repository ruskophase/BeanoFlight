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
            client.submit(CropPayload(job, image))
            thread.join(2.0)
            client.close()
            receiver.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed[0].job, job)
            np.testing.assert_array_equal(observed[0].image_bgr, image)


if __name__ == "__main__":
    unittest.main()
