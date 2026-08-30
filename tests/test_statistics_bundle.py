import math
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from beanoflight.detection import DetectorSettings
from beanoflight.statistics_bundle import (
    BundleSettings,
    _paired_features,
    _per_frame_workload,
    _score_appearance_outliers,
    _write_charts,
)
from beanoflight.statistics_features import (
    component_crop_mask,
    extract_view_features,
    foreground_mask,
    local_area_scale,
)


class StatisticsFeatureTests(unittest.TestCase):
    def test_foreground_component_is_aligned_to_native_crop(self):
        background = np.zeros((80, 100), dtype=np.uint8)
        current = background.copy()
        cv2.ellipse(current, (42, 31), (10, 7), 0, 0, 360, 180, -1)
        settings = DetectorSettings(
            blur_kernel=1,
            threshold=10,
            close_kernel=1,
            open_kernel=1,
            min_area_px=100,
            max_area_px=10_000,
            min_width_px=8,
            max_width_px=200,
            min_height_px=8,
            max_height_px=200,
        )

        foreground = foreground_mask(current, background, settings)
        mask = component_crop_mask(
            foreground,
            (84.0, 62.0),
            80,
            maximum_distance_px=10.0,
        )

        self.assertIsNotNone(mask)
        self.assertEqual(mask.shape, (80, 80))
        moments = cv2.moments(mask, binaryImage=True)
        self.assertAlmostEqual(moments["m10"] / moments["m00"], 40.0, delta=2.0)
        self.assertAlmostEqual(moments["m01"] / moments["m00"], 40.0, delta=2.0)

    def test_colour_shape_and_metric_features_use_only_silhouette(self):
        image = np.zeros((120, 120, 3), dtype=np.uint8)
        mask = np.zeros((120, 120), dtype=np.uint8)
        cv2.ellipse(mask, (60, 60), (24, 14), 25, 0, 360, 255, -1)
        image[mask > 0] = (40, 120, 210)
        measurement = extract_view_features(
            image, mask, area_scale_mm2_per_px=0.01
        )

        self.assertAlmostEqual(measurement.values["mean_b"], 40.0, delta=0.5)
        self.assertAlmostEqual(measurement.values["mean_g"], 120.0, delta=0.5)
        self.assertAlmostEqual(measurement.values["mean_r"], 210.0, delta=0.5)
        self.assertGreater(measurement.values["ellipse_aspect_ratio"], 1.5)
        self.assertAlmostEqual(
            measurement.values["area_mm2"],
            measurement.values["area_px"] * 0.01,
        )
        self.assertGreater(measurement.values["lab_a_mean"], 0.0)
        self.assertGreater(measurement.values["lab_b_mean"], 0.0)
        self.assertGreater(measurement.kernel_ms, 0.0)

    def test_local_area_scale_uses_jacobian_determinant(self):
        scale = local_area_scale(
            (10.0, 20.0), lambda point: (point[0] * 0.2, point[1] * 0.3)
        )
        self.assertAlmostEqual(scale, 0.06)

    def test_volume_proxies_are_dimensionally_consistent(self):
        left = {
            "area_mm2": math.pi * 4.0,
            "ellipse_major_mm": 6.0,
            "ellipse_minor_mm": 4.0,
            "lab_l_mean": 40.0,
            "lab_a_mean": 2.0,
            "lab_b_mean": 12.0,
        }
        right = dict(left)
        paired = _paired_features(left, right, 3.0)

        self.assertAlmostEqual(paired["equivalent_sphere_volume_proxy_mm3"], 4.0 / 3.0 * math.pi * 8.0)
        self.assertAlmostEqual(paired["rotational_ellipsoid_volume_proxy_mm3"], math.pi / 6.0 * 6.0 * 16.0)
        self.assertEqual(paired["projected_area_ratio_camr_to_caml"], 1.0)


class StatisticsBundleTests(unittest.TestCase):
    def test_settings_reject_odd_crop_or_too_many_samples(self):
        with self.assertRaises(ValueError):
            BundleSettings(crop_size_px=319).validate()
        with self.assertRaises(ValueError):
            BundleSettings(samples_per_bean=4).validate()

    def test_outlier_score_ranks_distinct_appearance(self):
        beans = []
        for index in range(20):
            unusual = index == 19
            beans.append(
                {
                    "combined_lab_l_mean": 85.0 if unusual else 40.0 + index * 0.05,
                    "combined_lab_a_mean": -20.0 if unusual else 1.0,
                    "combined_lab_b_mean": -15.0 if unusual else 12.0,
                    "combined_lab_chroma_mean": 5.0 if unusual else 12.1,
                    "combined_saturation_mean": 0.02 if unusual else 0.2,
                    "combined_red_chromaticity": 0.33 if unusual else 0.4,
                    "combined_green_chromaticity": 0.34 if unusual else 0.35,
                    "combined_blue_chromaticity": 0.33 if unusual else 0.25,
                }
            )

        _score_appearance_outliers(beans)

        self.assertGreater(
            beans[-1]["appearance_outlier_score"],
            max(bean["appearance_outlier_score"] for bean in beans[:-1]),
        )
        self.assertIn("light-low-chroma", beans[-1]["appearance_flags"])
        self.assertIn("appearance-outlier", beans[-1]["appearance_flags"])

    def test_empty_charts_are_still_reviewable_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_charts(root, [])
            self.assertTrue((root / "appearance-distributions.png").is_file())
            self.assertTrue((root / "size-and-volume.png").is_file())
            self.assertTrue((root / "view-agreement.png").is_file())

    def test_frame_workload_exposes_bursts_separately_from_one_job(self):
        rows = [
            {"frame_index": 2, "feature_kernel_ms": 6.0, "materialization_ms": 11.0},
            {"frame_index": 2, "feature_kernel_ms": 7.0, "materialization_ms": 12.0},
            {"frame_index": 4, "feature_kernel_ms": 5.0, "materialization_ms": 10.0},
        ]

        result = _per_frame_workload(rows, 6)

        self.assertEqual(result["active_sampled_frames"], 2)
        self.assertEqual(result["samples_per_source_frame"]["max"], 2.0)
        self.assertEqual(result["busiest_frame"]["frame_index"], 2)
        self.assertEqual(result["busiest_frame"]["feature_kernel_wall_ms"], 13.0)


if __name__ == "__main__":
    unittest.main()
