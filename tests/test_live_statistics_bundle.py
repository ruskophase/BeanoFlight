import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from beanoflight.live_statistics_bundle import (
    _compact_runtime_report,
    _derive_observation,
    _MeanColourCalibration,
    _score_live_appearance,
    _write_live_charts,
)


def _identity_calibration() -> _MeanColourCalibration:
    return _MeanColourCalibration(
        white_level=255.0,
        dark_level=0.0,
        white_balance_rgb=np.ones(3, dtype=np.float64),
        colour_matrix_rgb=np.eye(3, dtype=np.float64),
    )


class LiveStatisticsBundleTests(unittest.TestCase):
    def test_runtime_report_is_compacted_to_matching_run(self):
        report = {
            "schema": "beanoflight-performance-benchmark/v3",
            "runs": [
                {
                    "summary": {
                        "run_id": "wanted",
                        "frames_processed": 600,
                        "achieved_fps": 59.9,
                        "timings": {"large": [1, 2, 3]},
                    },
                    "outcome": {
                        "jobs_completed": 400,
                        "jobs_dropped": 0,
                        "beans": [{"large": "record"}],
                    },
                }
            ],
            "summaries": {"full": {"passed": True}},
            "system_telemetry": {
                "max_rss_mib": 500.0,
                "thermal_abort": False,
                "temperature_c": {
                    "cpu": {"max": 54.0},
                    "gpu": {"max": 52.0},
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            compact = _compact_runtime_report(path, "wanted")

        self.assertIsNotNone(compact)
        assert compact is not None
        self.assertEqual(compact["summary"]["frames_processed"], 600)
        self.assertEqual(compact["outcome"]["jobs_completed"], 400)
        self.assertEqual(compact["maximum_temperature_c"], 54.0)
        self.assertTrue(compact["acceptance_passed"])
        self.assertNotIn("timings", compact["summary"])
        self.assertNotIn("beans", compact["outcome"])

    def test_mean_colour_calibration_preserves_lightness_order(self):
        calibration = _identity_calibration()

        dark = calibration.transform_mean_bgr((20.0, 20.0, 20.0))
        light = calibration.transform_mean_bgr((180.0, 180.0, 180.0))

        self.assertLess(dark["approx_lab_l"], light["approx_lab_l"])
        self.assertAlmostEqual(dark["approx_lab_a"], 0.0, delta=1.0)
        self.assertAlmostEqual(dark["approx_lab_b"], 0.0, delta=1.0)

    def test_lab_conversion_retains_sub_display_code_precision(self):
        calibration = _identity_calibration()

        first = calibration.transform_mean_bgr((100.1, 110.1, 120.1))
        second = calibration.transform_mean_bgr((100.3, 110.3, 120.3))

        self.assertEqual(
            first["approx_calibrated_mean_r"],
            second["approx_calibrated_mean_r"],
        )
        self.assertNotEqual(first["approx_lab_l"], second["approx_lab_l"])

    def test_observation_derives_geometry_and_single_view_fallback(self):
        source = {
            "schema": "source",
            "caml_measurement_available": True,
            "camr_measurement_available": False,
            "mask_scale_to_native": 2.0,
            "caml_detection_area_px": 400.0,
            "caml_mask_area_px": 100.0,
            "caml_mask_variance_x_px2": 25.0,
            "caml_mask_variance_y_px2": 9.0,
            "caml_mask_covariance_xy_px2": 0.0,
            "caml_b_mean": 40.0,
            "caml_g_mean": 80.0,
            "caml_r_mean": 120.0,
        }

        row = _derive_observation(
            source,
            {"CamL": _identity_calibration(), "CamR": _identity_calibration()},
        )

        self.assertEqual(row["caml_area_native_px"], 400.0)
        self.assertEqual(row["caml_ellipse_minor_native_px"], 24.0)
        self.assertEqual(row["caml_ellipse_major_native_px"], 40.0)
        self.assertEqual(row["projected_area_proxy_view_count"], 1)
        self.assertEqual(row["projected_area_proxy_px"], 400.0)
        self.assertTrue(math.isfinite(row["equivalent_sphere_volume_proxy_px3"]))
        self.assertTrue(math.isfinite(row["rotational_ellipsoid_volume_proxy_px3"]))
        self.assertTrue(math.isfinite(row["combined_approx_lab_l"]))

    def test_two_sd_screen_selects_distinct_dark_tail(self):
        beans = []
        for index in range(40):
            lightness = 50.0 + (index % 5 - 2) * 0.2
            beans.append(
                {
                    "combined_approx_lab_l_mean": lightness,
                    "combined_approx_lab_a_mean": 4.0,
                    "combined_approx_lab_b_mean": 12.0,
                }
            )
        beans.append(
            {
                "combined_approx_lab_l_mean": 20.0,
                "combined_approx_lab_a_mean": 3.0,
                "combined_approx_lab_b_mean": 10.0,
            }
        )

        summary = _score_live_appearance(beans)

        self.assertEqual(summary["candidate_count"], 1)
        self.assertFalse(any(bean["dark_candidate_2sd"] for bean in beans[:-1]))
        self.assertTrue(beans[-1]["dark_candidate_2sd"])
        self.assertLess(
            beans[-1]["lightness_z_score"],
            -2.0,
        )

    def test_empty_charts_are_reviewable(self):
        summary = {
            "lightness_mean": math.nan,
            "lightness_sample_standard_deviation": math.nan,
            "threshold_mean_minus_2sd": math.nan,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_live_charts(root, [], summary)
            self.assertEqual(
                {path.name for path in root.glob("*.png")},
                {
                    "appearance-distributions.png",
                    "dark-bean-candidates.png",
                    "size-and-volume.png",
                    "view-agreement.png",
                },
            )


if __name__ == "__main__":
    unittest.main()
