import unittest

import cv2
import numpy as np

from beanoflight.detection import BeanDetector, DetectorError, DetectorSettings
from beanoflight.display import render_pipeline_stage


class DetectionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.background = np.full((180, 240, 3), 18, dtype=np.uint8)
        self.frame = self.background.copy()
        cv2.ellipse(self.frame, (120, 65), (24, 17), 20, 0, 360, (205, 190, 170), -1)

    def test_detects_bean_and_exposes_every_named_stage(self):
        detector = BeanDetector(
            DetectorSettings(
                threshold=15,
                min_area_px=900,
                max_area_px=5_000,
                min_width_px=10,
                max_width_px=100,
                min_height_px=10,
                max_height_px=100,
            )
        )
        result = detector.inspect(self.frame, self.background)
        self.assertEqual(len(result.detections), 1)
        centre = result.detections[0].centroid_px
        self.assertAlmostEqual(centre[0], 120.0, delta=2.0)
        self.assertAlmostEqual(centre[1], 65.0, delta=2.0)
        self.assertGreater(result.detections[0].area_px, 900)
        self.assertEqual(
            [stage.key for stage in result.stages],
            [
                "input",
                "grayscale",
                "blur",
                "difference",
                "threshold",
                "close",
                "open",
                "dilate",
                "components",
                "filtered",
            ],
        )
        threshold = next(stage for stage in result.stages if stage.key == "threshold")
        self.assertIn("threshold=15", threshold.settings)
        rendered = render_pipeline_stage(threshold)
        self.assertEqual(rendered.shape, self.frame.shape)

    def test_rejected_component_remains_visible_in_final_debug_stage(self):
        settings = DetectorSettings(min_area_px=5_000, max_area_px=10_000)
        result = BeanDetector(settings).inspect(self.frame, self.background)
        self.assertEqual(result.detections, ())
        self.assertEqual(result.stages[-1].key, "filtered")

    def test_even_morphology_kernel_is_rejected(self):
        with self.assertRaisesRegex(DetectorError, "odd"):
            DetectorSettings(close_kernel=4).validate()


if __name__ == "__main__":
    unittest.main()
