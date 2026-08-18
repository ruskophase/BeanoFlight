import argparse
import unittest

from beanoflight.performance_benchmark import _scenario_summaries, _scenarios


class PerformanceBenchmarkTests(unittest.TestCase):
    def test_scenarios_are_validated_and_deduplicated(self):
        self.assertEqual(_scenarios("core,full,core"), ("core", "full"))
        with self.assertRaises(argparse.ArgumentTypeError):
            _scenarios("visual")

    def test_scenario_summary_reports_repeat_stability(self):
        runs = [
            {
                "scenario": "full",
                "summary": {
                    "achieved_fps": fps,
                    "mean_processing_ms": analysis,
                    "crops_submitted": 1,
                },
                "outcome": {
                    "settled": True,
                    "jobs": 1,
                    "jobs_completed": 1,
                    "jobs_dropped": 0,
                    "jobs_failed": 0,
                    "decisions": 1,
                    "beans_with_jobs": 1,
                },
            }
            for fps, analysis in ((60.0, 11.0), (59.98, 11.2), (60.01, 10.9))
        ]
        summary = _scenario_summaries(runs, 60.0)["full"]
        self.assertAlmostEqual(summary["minimum_fps"], 59.98)
        self.assertEqual(summary["minimum_acceptable_fps"], 59.0)
        self.assertTrue(summary["all_within_one_fps_of_target"])
        self.assertTrue(summary["all_outcomes_complete"])
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["fps"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
