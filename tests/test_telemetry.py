import unittest

from beanoflight.telemetry import (
    SystemTelemetrySampler,
    TimingAccumulator,
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
        sampler = SystemTelemetrySampler(interval_seconds=1.0)
        sampler.start()
        summary = sampler.stop()
        self.assertGreaterEqual(summary["samples"], 2)
        self.assertGreaterEqual(summary["process_cpu_seconds"], 0.0)
        self.assertIn("temperature_c", summary)


if __name__ == "__main__":
    unittest.main()
