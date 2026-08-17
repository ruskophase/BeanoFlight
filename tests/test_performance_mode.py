import unittest

from beanoflight.cli import build_parser as flight_parser
from beanoflight.mock_inferencer_app import parser as inferencer_parser
from beanoflight.registry_monitor_app import parser as monitor_parser
from beanoflight.registry_service import parser as registry_parser
from beanoflight.simulation_launcher_app import performance_mode_arguments
from beanoflight.sorter_app import parser as sorter_parser


class PerformanceModeTests(unittest.TestCase):
    def test_launcher_applies_every_low_overhead_component_flag(self):
        self.assertEqual(performance_mode_arguments("registry", True), ("--quiet",))
        self.assertEqual(
            performance_mode_arguments("monitor", True), ("--no-live-updates",)
        )
        self.assertEqual(
            performance_mode_arguments("inferencer", True),
            ("--no-crop-preview", "--no-activity-log"),
        )
        self.assertEqual(
            performance_mode_arguments("sorter", True),
            ("--no-gate-animation", "--no-activity-log"),
        )
        self.assertEqual(
            performance_mode_arguments("flight", True), ("--performance-mode",)
        )

    def test_visual_mode_adds_no_overrides(self):
        for component in ("registry", "monitor", "inferencer", "sorter", "flight"):
            self.assertEqual(performance_mode_arguments(component, False), ())

    def test_component_parsers_accept_launcher_flags(self):
        self.assertTrue(registry_parser().parse_args(["--quiet"]).quiet)
        self.assertTrue(
            monitor_parser().parse_args(["--no-live-updates"]).no_live_updates
        )

        inferencer = inferencer_parser().parse_args(
            ["--no-crop-preview", "--no-activity-log"]
        )
        self.assertTrue(inferencer.no_crop_preview)
        self.assertTrue(inferencer.no_activity_log)

        sorter = sorter_parser().parse_args(
            ["--no-gate-animation", "--no-activity-log"]
        )
        self.assertTrue(sorter.no_gate_animation)
        self.assertTrue(sorter.no_activity_log)
        self.assertTrue(
            flight_parser().parse_args(["--performance-mode"]).performance_mode
        )


if __name__ == "__main__":
    unittest.main()
