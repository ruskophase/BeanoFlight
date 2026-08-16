import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from beanoflight.crop import CropPayload, extract_square_crop
from beanoflight.inference_transport import ZeroMQCropClient, ZeroMQCropReceiver
from beanoflight.models import BeanRef
from beanoflight.registry_models import InferenceJob, InferenceStatus


class CropTests(unittest.TestCase):
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
