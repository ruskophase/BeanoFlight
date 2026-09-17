import unittest
from types import SimpleNamespace

from beanoflight.actuator_app import parser as actuator_parser
from beanoflight.cli import build_parser as flight_parser
from beanoflight.mock_inferencer_app import parser as inferencer_parser
from beanoflight.registry_monitor_app import parser as monitor_parser
from beanoflight.registry_service import parser as registry_parser
from beanoflight.simulation_launcher_app import (
    SimulationLauncherApp,
    performance_mode_arguments,
)
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
            performance_mode_arguments("actuator", True),
            ("--no-activity-log",),
        )
        self.assertEqual(
            performance_mode_arguments("sorter", True),
            (
                "--no-gate-animation",
                "--no-activity-log",
                "--suppress-cyclic-gc",
            ),
        )
        self.assertEqual(
            performance_mode_arguments("flight", True), ("--performance-mode",)
        )

    def test_visual_mode_adds_no_overrides(self):
        for component in (
            "registry",
            "monitor",
            "inferencer",
            "actuator",
            "sorter",
            "flight",
        ):
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

        self.assertTrue(
            actuator_parser().parse_args(["--no-activity-log"]).no_activity_log
        )

        sorter = sorter_parser().parse_args(
            [
                "--no-gate-animation",
                "--no-activity-log",
                "--suppress-cyclic-gc",
            ]
        )
        self.assertTrue(sorter.no_gate_animation)
        self.assertTrue(sorter.no_activity_log)
        self.assertTrue(sorter.suppress_cyclic_gc)
        self.assertTrue(
            flight_parser().parse_args(["--performance-mode"]).performance_mode
        )

    def test_sorter_diagnostics_are_opt_in(self):
        defaults = sorter_parser().parse_args([])
        self.assertFalse(defaults.gate_animation)
        self.assertFalse(defaults.activity_log)

        enabled = sorter_parser().parse_args(["--gate-animation", "--activity-log"])
        self.assertTrue(enabled.gate_animation)
        self.assertTrue(enabled.activity_log)

    def test_launcher_keeps_registry_alive_until_workers_exit(self):
        calls = []

        class Process:
            def __init__(self, name):
                self.name = name
                self.running = True

            def poll(self):
                return None if self.running else 0

            def terminate(self):
                calls.append(("terminate", self.name))

            def wait(self, *, timeout):
                calls.append(("wait", self.name, timeout))
                self.running = False

            def kill(self):
                raise AssertionError("orderly shutdown should not require kill")

        status = SimpleNamespace(set=lambda value: calls.append(("status", value)))
        launcher = SimpleNamespace(
            _processes={
                "registry": Process("registry"),
                "inferencer": Process("inferencer"),
                "flight": Process("flight"),
            },
            _external_registry=False,
            status_var=status,
        )

        SimulationLauncherApp.stop_all(launcher)

        registry_stop = calls.index(("terminate", "registry"))
        self.assertLess(calls.index(("wait", "inferencer", 5.0)), registry_stop)
        self.assertLess(calls.index(("wait", "flight", 5.0)), registry_stop)
        self.assertEqual(launcher._processes, {})


if __name__ == "__main__":
    unittest.main()
