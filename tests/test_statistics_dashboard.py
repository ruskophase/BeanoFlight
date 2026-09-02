import json
import tempfile
import unittest
from pathlib import Path

from beanoflight.statistics_dashboard import (
    DASHBOARD_SCHEMA,
    write_statistics_dashboard,
)


class StatisticsDashboardTests(unittest.TestCase):
    def test_dashboard_is_self_contained_and_json_safe(self):
        beans = [
            {
                "bean_id": "run:1",
                "bean_sequence": 1,
                "first_frame_index": 100,
                "sample_count": 2,
                "combined_approx_lab_l_mean": 42.5,
                "combined_approx_lab_a_mean": 3.0,
                "combined_approx_lab_b_mean": 8.0,
                "combined_approx_lab_chroma_mean": 8.54,
                "caml_approx_calibrated_mean_r_median": 80.0,
                "caml_approx_calibrated_mean_g_median": 60.0,
                "caml_approx_calibrated_mean_b_median": 40.0,
                "camr_approx_calibrated_mean_r_median": 82.0,
                "camr_approx_calibrated_mean_g_median": 62.0,
                "camr_approx_calibrated_mean_b_median": 42.0,
                "combined_approx_lab_l_mean_unused": float("nan"),
                "dark_candidate_2sd": True,
                "minimum_measurement_view_count": 2,
            },
            {
                "bean_id": "run:2",
                "bean_sequence": 2,
                "first_frame_index": 106,
                "sample_count": 1,
                "combined_approx_lab_l_mean": float("nan"),
                "minimum_measurement_view_count": 1,
                "enrichment_fallback_observation_count": 1,
            },
        ]
        summary = {
            "source_run_id": "run",
            "counts": {"confirmed_beans": 2},
            "test_non_finite": float("nan"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dashboard"
            descriptor = write_statistics_dashboard(
                output,
                beans=beans,
                summary=summary,
                source_fps=60.0,
            )

            self.assertEqual(descriptor["bean_rows"], 2)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "index.html",
                    "chart.html",
                    "dashboard.css",
                    "dashboard.js",
                    "batch-data.js",
                },
            )
            html = (output / "index.html").read_text(encoding="utf-8")
            chart_html = (output / "chart.html").read_text(encoding="utf-8")
            script = (output / "dashboard.js").read_text(encoding="utf-8")
            self.assertNotIn("https://", html + chart_html + script)
            self.assertNotIn("http://", html + chart_html + script)
            self.assertIn('href="index.html#overview"', html)
            self.assertIn('href="chart.html#volume"', html)
            assignment = (output / "batch-data.js").read_text(encoding="utf-8")
            data = json.loads(
                assignment.removeprefix("window.BEANO_BATCH_DATA=").removesuffix(";\n")
            )
            self.assertEqual(data["schema"], DASHBOARD_SCHEMA)
            self.assertEqual(len(data["beans"]), 2)
            self.assertIsNone(data["summary"]["test_non_finite"])
            fields = {name: index for index, name in enumerate(data["fields"])}
            self.assertEqual(data["beans"][0][fields["red"]], 81.0)
            self.assertEqual(data["beans"][0][fields["caml_red"]], 80.0)
            self.assertEqual(data["beans"][0][fields["camr_red"]], 82.0)
            self.assertIsNone(data["beans"][1][fields["lightness"]])
            self.assertTrue(data["beans"][1][fields["enrichment_fallback"]])


if __name__ == "__main__":
    unittest.main()
