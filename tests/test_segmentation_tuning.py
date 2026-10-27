import unittest

import cv2
import numpy as np

from beanoflight.segmentation_tuning import _metric, _summarize, select_cohorts


class SegmentationTuningTests(unittest.TestCase):
    def test_split_is_reported_without_guessing_identity(self):
        foreground = np.zeros((120, 120), np.uint8)
        cv2.circle(foreground, (43, 60), 10, 255, -1)
        cv2.circle(foreground, (77, 60), 10, 255, -1)
        original_area = cv2.countNonZero(foreground) * 4
        observation = {
            "bean_sequence": 1,
            "frame_index": 9,
            "source_crop_size_px": 120,
            "caml_centroid_x_px": 120.0,
            "caml_centroid_y_px": 120.0,
            "caml_area_px": original_area,
            "caml_area_mm2": 60.0,
        }
        result, visual = _metric(observation, "CamL", "close3", "seed", foreground)
        self.assertIsNone(result.area_px)
        self.assertTrue(result.split_candidate)
        self.assertEqual(len(result.nearby_component_areas_px), 2)
        self.assertIsNotNone(visual)
        self.assertEqual(set(np.unique(visual)), {0, 1, 2})

    def test_summary_counts_unassigned_splits(self):
        foreground = np.zeros((120, 120), np.uint8)
        cv2.circle(foreground, (43, 60), 10, 255, -1)
        cv2.circle(foreground, (77, 60), 10, 255, -1)
        original_area = cv2.countNonZero(foreground) * 4
        observation = {
            "bean_sequence": 1, "frame_index": 9, "source_crop_size_px": 120,
            "caml_centroid_x_px": 120.0, "caml_centroid_y_px": 120.0,
            "caml_area_px": original_area, "caml_area_mm2": 60.0,
        }
        split, _ = _metric(observation, "CamL", "close3", "seed", foreground)
        from dataclasses import replace
        baseline = replace(split, variant="baseline", area_px=original_area, split_candidate=False)
        summary = _summarize([baseline, split])
        self.assertEqual(summary["close3"]["split_without_target_identity"], 1)

    def test_cohorts_keep_named_examples(self):
        beans = []
        for number in range(1, 12):
            beans.append({
                "bean_sequence": number,
                "sample_count": 3,
                "projected_area_geomean_mm2_median": 30.0 + number,
                "projected_area_ratio_camr_to_caml_median": 1.0,
                "first_frame_index": number * 10,
            })
        cohorts = select_cohorts(beans, seeds=(2, 7), suspicious_count=2, control_count=2)
        self.assertEqual(cohorts[2], "seed")
        self.assertEqual(cohorts[7], "seed")
        self.assertEqual(sum(value == "suspect" for value in cohorts.values()), 2)


if __name__ == "__main__":
    unittest.main()
