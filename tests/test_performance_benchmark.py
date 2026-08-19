import argparse
import unittest

from beanoflight.performance_benchmark import _scenario_summaries, _scenarios
from beanoflight.performance_benchmark import parser as benchmark_parser
from beanoflight.system_test import parser as system_test_parser


class PerformanceBenchmarkTests(unittest.TestCase):
    def test_hardware_actuator_is_opt_in(self):
        default = benchmark_parser().parse_args(
            ["/recording", "--background-frames", "1,2,3"]
        )
        enabled = benchmark_parser().parse_args(
            [
                "/recording",
                "--background-frames",
                "1,2,3",
                "--esp32-actuator",
                "--esp32-port",
                "/dev/test-actuator",
            ]
        )
        self.assertFalse(default.esp32_actuator)
        self.assertTrue(enabled.esp32_actuator)
        self.assertEqual(enabled.esp32_port, "/dev/test-actuator")

    def test_scenarios_are_validated_and_deduplicated(self):
        self.assertEqual(_scenarios("core,full,core"), ("core", "full"))
        with self.assertRaises(argparse.ArgumentTypeError):
            _scenarios("visual")

    def test_adaptive_edge_resize_is_enabled_unless_explicitly_disabled(self):
        benchmark = benchmark_parser().parse_args(
            ["recording", "--background-frames", "1,2,3"]
        )
        system_test = system_test_parser().parse_args(
            ["recording", "--background-frames", "1,2,3"]
        )
        self.assertFalse(benchmark.no_adaptive_edge_resize)
        self.assertFalse(system_test.no_adaptive_edge_resize)

        benchmark = benchmark_parser().parse_args(
            [
                "recording",
                "--background-frames",
                "1,2,3",
                "--no-adaptive-edge-resize",
            ]
        )
        system_test = system_test_parser().parse_args(
            [
                "recording",
                "--background-frames",
                "1,2,3",
                "--no-adaptive-edge-resize",
            ]
        )
        self.assertTrue(benchmark.no_adaptive_edge_resize)
        self.assertTrue(system_test.no_adaptive_edge_resize)

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
