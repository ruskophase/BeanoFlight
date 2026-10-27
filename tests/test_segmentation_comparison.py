import unittest

from beanoflight.segmentation_comparison import _metrics, _seed_events
from beanoflight.segmentation_tuning import SEEDS


class SegmentationComparisonTests(unittest.TestCase):
    def test_one_original_track_can_have_two_nearby_alternatives(self):
        baseline_beans = [
            {"bean_id": f"old/{number}", "bean_sequence": number, "photo_frame_index": number * 10}
            for number in SEEDS
        ]
        baseline_obs = [
            {"bean_id": f"old/{number}", "frame_index": number * 10,
             "caml_centroid_x_px": 100.0, "caml_centroid_y_px": 100.0}
            for number in SEEDS
        ]
        alternative_beans = [
            {"bean_id": f"new/{number}/{side}", "bean_sequence": number * 2 + side,
             "projected_area_geomean_mm2_median": 30.0}
            for number in SEEDS for side in (0, 1)
        ]
        alternative_obs = [
            {"bean_id": f"new/{number}/{side}", "bean_sequence": number * 2 + side,
             "frame_index": number * 10, "caml_centroid_x_px": 70.0 + side * 60,
             "caml_centroid_y_px": 100.0, "caml_area_mm2": 30.0, "camr_area_mm2": 31.0}
            for number in SEEDS for side in (0, 1)
        ]
        events = _seed_events(baseline_beans, baseline_obs, alternative_beans, alternative_obs)
        self.assertEqual(len(events[120]), 2)
        self.assertEqual({item["bean_id"] for item in events[120]}, {"new/120/0", "new/120/1"})

    def test_comparison_reports_coverage_alongside_outliers(self):
        beans = [
            {"bean_id": "run/1", "sample_count": 2,
             "projected_area_geomean_mm2_median": 75,
             "projected_area_ratio_camr_to_caml_median": 1.5},
            {"bean_id": "run/2", "sample_count": 1,
             "projected_area_geomean_mm2_median": 30,
             "projected_area_ratio_camr_to_caml_median": 1.0},
        ]
        observations = [
            {"bean_id": "run/1", "projected_area_geomean_mm2": 40},
            {"bean_id": "run/1", "projected_area_geomean_mm2": 80},
            {"bean_id": "run/2", "projected_area_geomean_mm2": 30},
        ]
        metrics = _metrics(beans, observations, {})
        self.assertEqual(metrics["confirmed_beans"], 2)
        self.assertEqual(metrics["beans_with_one_sample"], 1)
        self.assertEqual(metrics["area_over_70_mm2"], 1)
        self.assertEqual(metrics["stereo_ratio_outside_0_75_to_1_33"], 1)
        self.assertEqual(metrics["temporal_area_ratio_over_1_5"], 1)


if __name__ == "__main__":
    unittest.main()
