import unittest

import numpy as np

from beanoflight.tensorrt_inference import (
    TensorRTInferenceError,
    _prepare_rgb_chw,
)


class TensorRTInferenceHelpersTests(unittest.TestCase):
    def test_preprocessing_converts_bgr_to_scaled_rgb_chw(self):
        image = np.zeros((224, 224, 3), dtype=np.uint8)
        image[:, :, 0] = 10
        image[:, :, 1] = 20
        image[:, :, 2] = 30
        destination = np.empty((3, 224, 224), dtype=np.float32)

        _prepare_rgb_chw(image, destination)

        self.assertAlmostEqual(float(destination[0, 0, 0]), 30 / 255)
        self.assertAlmostEqual(float(destination[1, 0, 0]), 20 / 255)
        self.assertAlmostEqual(float(destination[2, 0, 0]), 10 / 255)

    def test_preprocessing_rejects_non_bgr_input(self):
        with self.assertRaises(TensorRTInferenceError):
            _prepare_rgb_chw(
                np.zeros((224, 224), dtype=np.uint8),
                np.empty((3, 224, 224), dtype=np.float32),
            )


if __name__ == "__main__":
    unittest.main()
