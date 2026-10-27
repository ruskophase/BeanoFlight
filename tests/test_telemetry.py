import unittest
from unittest.mock import patch

from beanoflight.telemetry import (
    SystemTelemetrySampler,
    TimingAccumulator,
    current_rss_mib,
    summarize_samples,
)


class TelemetryTests(unittest.TestCase):
    def test_sample_summary_has_stable_percentiles(self):
        summary = summarize_samples((1.0, 2.0, 3.0, 4.0, 100.0))
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["p50"], 3.0)
        self.assertEqual(summary["p95"], 100.0)
        self.assertEqual(summary["max"], 100.0)

    def test_timing_accumulator_is_bounded_and_resettable(self):
        accumulator = TimingAccumulator(capacity=2)
        for value in (1.0, 2.0, 3.0):
            accumulator.add(value)
        self.assertEqual(accumulator.summary()["mean"], 2.5)
        accumulator.clear()
        self.assertEqual(accumulator.summary()["count"], 0)

    def test_system_sampler_returns_portable_process_metrics(self):
        sampler = SystemTelemetrySampler(
            interval_seconds=1.0,
            watched_pids={"self": 0},
        )
        sampler.start()
        summary = sampler.stop()
        self.assertGreaterEqual(summary["samples"], 2)
        self.assertGreaterEqual(summary["process_cpu_seconds"], 0.0)
        self.assertIn("temperature_c", summary)

    def test_current_rss_is_available_for_this_process(self):
        self.assertGreater(current_rss_mib(), 0.0)

    def test_sampler_sets_thermal_abort_at_configured_limit(self):
        sample = {
            "monotonic_ns": 1,
            "load_1m": 0.0,
            "temperatures_c": {"test-zone": 65.1},
            "cpu_frequency_mhz": (),
        }
        sampler = SystemTelemetrySampler(maximum_temperature_c=65.0)
        with patch("beanoflight.telemetry._system_sample", return_value=sample):
            sampler.start()
            summary = sampler.stop()

        self.assertTrue(sampler.thermal_abort.is_set())
        self.assertTrue(summary["thermal_abort"])
        self.assertIn("test-zone", summary["thermal_abort_detail"])


if __name__ == "__main__":
    unittest.main()
