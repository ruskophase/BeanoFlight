import argparse
import unittest
from types import SimpleNamespace

from beanoflight.models import BeanRef, Detection, Observation
from beanoflight.performance_benchmark import (
    _compact_endurance_outcome,
    _identity_continuity,
    _list_run_records,
    _retry_registry_call,
    _scenario_summaries,
    _scenarios,
    _soak_acceptance,
)
from beanoflight.performance_benchmark import parser as benchmark_parser
from beanoflight.registry_zmq import RegistryTransportError
from beanoflight.system_test import parser as system_test_parser


class PerformanceBenchmarkTests(unittest.TestCase):
    def test_long_outcome_records_are_collected_in_bounded_pages(self):
        records = tuple(
            SimpleNamespace(bean_ref=BeanRef("long-run", sequence))
            for sequence in range(1, 206)
        )

        class Client:
            def __init__(self):
                self.calls = []

            def list_records_page(
                self, *, run_id, after_sequence, limit
            ):
                self.calls.append((run_id, after_sequence, limit))
                return tuple(
                    record
                    for record in records
                    if record.bean_ref.sequence > after_sequence
                )[:limit]

        client = Client()
        collected, retries = _list_run_records(client, "long-run")

        self.assertEqual(collected, records)
        self.assertEqual(retries, 0)
        self.assertEqual(
            client.calls,
            [
                ("long-run", 0, 100),
                ("long-run", 100, 100),
                ("long-run", 200, 100),
            ],
        )

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

    def test_inference_backend_can_be_selected(self):
        arguments = benchmark_parser().parse_args(
            [
                "/recording",
                "--background-frames",
                "1,2,3",
                "--inference-backend",
                "mock",
                "--inference-engine",
                "/tmp/test.engine",
            ]
        )
        self.assertEqual(arguments.inference_backend, "mock")
        self.assertEqual(str(arguments.inference_engine), "/tmp/test.engine")

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

    def test_soak_mode_and_clock_barrier_are_configurable(self):
        benchmark = benchmark_parser().parse_args(
            [
                "recording",
                "--background-frames",
                "1,2,3",
                "--soak-runs",
                "12",
                "--clock-start-lead-ms",
                "75",
                "--maximum-clock-offset-ms",
                "1.5",
            ]
        )
        system_test = system_test_parser().parse_args(
            [
                "recording",
                "--background-frames",
                "1,2,3",
                "--clock-start-lead-ms",
                "75",
                "--maximum-clock-offset-ms",
                "1.5",
            ]
        )
        self.assertEqual(benchmark.soak_runs, 12)
        self.assertEqual(benchmark.clock_start_lead_ms, 75)
        self.assertEqual(system_test.clock_start_lead_ms, 75)
        self.assertEqual(system_test.maximum_clock_offset_ms, 1.5)

    def test_endurance_mode_and_thermal_limit_are_configurable(self):
        arguments = benchmark_parser().parse_args(
            [
                "recording",
                "--background-frames",
                "1,2,3",
                "--endurance-minutes",
                "60",
                "--maximum-temperature-c",
                "64",
            ]
        )
        self.assertEqual(arguments.endurance_minutes, 60.0)
        self.assertEqual(arguments.maximum_temperature_c, 64.0)

    def test_endurance_outcome_discards_large_per_bean_marks(self):
        outcome = {
            "beans": 1,
            "timing_ledger": {
                "results": {"scheduled": 1},
                "per_bean": [
                    {
                        "bean_id": "run:1",
                        "sequence": 1,
                        "result": "scheduled",
                        "classification": {"sample_count": 3},
                        "marks_ns": {"large": 123},
                        "durations_ms": {"path": 4.0},
                    }
                ],
            },
        }

        compact = _compact_endurance_outcome(outcome)

        self.assertEqual(compact["beans"], 1)
        bean = compact["timing_ledger"]["per_bean"][0]
        self.assertEqual(bean["classification"]["sample_count"], 3)
        self.assertNotIn("marks_ns", bean)

    def test_registry_read_retries_a_transient_transport_timeout(self):
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RegistryTransportError("temporary pause")
            return {"healthy": True}

        result, retries = _retry_registry_call(operation)

        self.assertEqual(result, {"healthy": True})
        self.assertEqual(retries, 2)

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

    def test_hardware_summary_fails_if_an_actuation_fails(self):
        runs = [
            {
                "scenario": "full",
                "summary": {
                    "achieved_fps": 60.0,
                    "mean_processing_ms": 5.0,
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
                    "actuations_failed": 1,
                },
            }
        ]

        simulated = _scenario_summaries(runs, 60.0)["full"]
        hardware = _scenario_summaries(
            runs, 60.0, require_successful_actuations=True
        )["full"]

        self.assertTrue(simulated["passed"])
        self.assertFalse(hardware["all_outcomes_complete"])
        self.assertFalse(hardware["passed"])

    def test_soak_acceptance_enforces_evidence_clock_and_identity(self):
        run = {
            "scenario": "full",
            "summary": {
                "achieved_fps": 60.0,
                "frames_skipped": 0,
                "missed_deadlines": 0,
                "crops_dropped": 0,
                "clock_synchronized": True,
                "clock_start_offset_ms": 0.1,
                "clock_anchor_misses": 0,
                "timings": {"crop_selection": {"stereo_enabled": True}},
            },
            "outcome": {
                "beans": 2,
                "jobs_completed": 6,
                "jobs_dropped": 0,
                "jobs_failed": 0,
                "classification_evidence": 6,
                "stereo_pairs_complete": 6,
                "stereo_pairs_incomplete": 0,
                "stereo_pairing": {"maximum_synchronization_delta_ns": 0},
                "settled": True,
                "clock_consistency": {"all_consistent": True},
                "identity_continuity": {"suspected_fragments": 0},
                "timing_ledger": {
                    "results": {"too_late": 0},
                    "per_bean": [
                        {
                            "classification": {
                                "sample_count": 3,
                                "expected_samples": 3,
                            }
                        },
                        {
                            "classification": {
                                "sample_count": 3,
                                "expected_samples": 3,
                            }
                        },
                    ],
                },
            },
        }

        passed = _soak_acceptance(
            [run, run],
            target_fps=60.0,
            minimum_three_sample_rate=0.95,
            minimum_samples_per_bean=2,
            expected_beans=2,
        )
        failed = _soak_acceptance(
            [
                {
                    **run,
                    "outcome": {
                        **run["outcome"],
                        "identity_continuity": {"suspected_fragments": 1},
                    },
                }
            ],
            target_fps=60.0,
            minimum_three_sample_rate=0.95,
            minimum_samples_per_bean=2,
            expected_beans=2,
        )

        self.assertTrue(passed["passed"])
        self.assertFalse(failed["passed"])
        self.assertFalse(failed["checks"]["zero_suspected_duplicate_ids"])

    def test_identity_continuity_detects_adjacent_track_fragment(self):
        def observation(frame, timestamp_ns, x_mm, y_mm):
            return Observation(
                frame,
                timestamp_ns,
                Detection((100.0, 100.0), (80, 80, 40, 40), 2_000, 0.9, (0, 0, 0)),
                (x_mm, y_mm),
            )

        earlier_history = (
            observation(420, 0, 16.48, -35.50),
            observation(421, 16_666_000, 16.65, -29.54),
        )
        later_history = (
            observation(422, 33_332_000, 16.84, -16.32),
            observation(423, 49_998_000, 16.86, -0.07),
        )
        records = (
            SimpleNamespace(
                bean_ref=BeanRef("fragment-run", 105),
                track=SimpleNamespace(
                    history=earlier_history,
                    state=(16.65, -29.54, 0.0, 400.0),
                ),
                prediction=None,
            ),
            SimpleNamespace(
                bean_ref=BeanRef("fragment-run", 106),
                track=SimpleNamespace(
                    history=later_history,
                    state=(16.86, -0.07, 0.0, 900.0),
                ),
                prediction=None,
            ),
        )

        result = _identity_continuity(records)

        self.assertEqual(result["suspected_fragments"], 1)
        self.assertEqual(result["suspects"][0]["frame_gap"], 1)


if __name__ == "__main__":
    unittest.main()
