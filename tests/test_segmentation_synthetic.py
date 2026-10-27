import unittest

import cv2
import numpy as np

from beanoflight.segmentation_synthetic import _composite, _evaluate


class SyntheticSegmentationTests(unittest.TestCase):
    def test_known_separate_components_score_accurately(self):
        background = np.zeros((120, 160), np.uint8)
        left = np.full((20, 20), 100, np.uint8)
        right = np.full((20, 20), 150, np.uint8)
        mask = np.zeros((20, 20), np.uint8)
        cv2.circle(mask, (10, 10), 8, 1, -1)
        image, truth_left, truth_right = _composite(
            background, (left, mask), (right, mask), 6
        )
        self.assertGreater(cv2.countNonZero(image), 0)
        foreground = np.asarray((truth_left | truth_right) > 0, np.uint8) * 255
        measured = _evaluate(foreground, truth_left, truth_right)
        self.assertTrue(measured["separated"])
        self.assertTrue(measured["accurate"])

    def test_fused_component_does_not_count_as_separated(self):
        left = np.zeros((80, 80), np.uint8)
        right = np.zeros_like(left)
        cv2.circle(left, (25, 40), 10, 1, -1)
        cv2.circle(right, (55, 40), 10, 1, -1)
        fused = np.asarray((left | right) > 0, np.uint8) * 255
        cv2.line(fused, (35, 40), (45, 40), 255, 2)
        measured = _evaluate(fused, left, right)
        self.assertFalse(measured["separated"])


if __name__ == "__main__":
    unittest.main()
