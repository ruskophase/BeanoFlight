import io
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from beanoflight.live_clock import LiveClockModel, ClockMappingError
from beanoflight.live_source import FastCapLiveProducer
from beanoflight.models import BeanRef
from beanoflight.registry_models import (RunSession, RunState, SortingDecision,
    GateWindow, run_session_from_dict, run_session_to_dict)
from beanoflight.sorter import _pending_actuation, _external_actuation_plan


def session():
    return RunSession('clock-test', 1, RunState.RUNNING, '/test', 'live', 36000,
        59.5, 59.5, 100_000_000_000, 100_000_000_000, 80_000_000_000,
        False, 0, 0, {})


class LiveClockTests(unittest.TestCase):
    def test_ten_minutes_positive_and_negative_drift(self):
        for ppm in (-50, 50, 500, -500):
            model = LiveClockModel()
            source, host = 100_000_000_000, 80_000_000_000
            host_rate = 1 + ppm / 1_000_000
            for second in range(601):
                s = source + second * 1_000_000_000
                h = host + round(second * 1_000_000_000 * host_rate)
                model.observe(s, h, 100)
                snap = model.snapshot(h + 25_000_000)
                if second:
                    # A frame delivered 25 ms later remains 25 ms old;
                    # the same map also predicts a valve crossing 100 ms ahead.
                    self.assertLess(abs(snap.source_to_monotonic_ns(s + 100_000_000)
                        - (h + round(100_000_000 * host_rate))), 100)
                    self.assertEqual(h + 25_000_000 - snap.source_to_monotonic_ns(s), 25_000_000)

    def test_rate_change_is_followed_without_long_run_error(self):
        model = LiveClockModel()
        s, h = 100_000_000_000, 80_000_000_000
        model.observe(s, h, 100)
        for second in range(1, 601):
            increment = 1_000_050_000 if second < 300 else 999_950_000
            s += 1_000_000_000
            h += increment
            model.observe(s, h, 100)
            snap = model.snapshot(h + 900_000_000)
            self.assertLess(abs(snap.source_to_monotonic_ns(s+900_000_000)
                - (h + round(increment * .9))), 100_000)

    def test_stale_imprecise_reversed_and_jumped_samples_fail_closed(self):
        for bad in ((102_000_000_000, 82_000_000_000, 50_001),
                    (99_000_000_000, 82_000_000_000, 100),
                    (102_000_000_000, 82_010_000_000, 100)):
            model = LiveClockModel()
            model.observe(100_000_000_000, 80_000_000_000, 100)
            with self.assertRaises(ClockMappingError): model.observe(*bad)
            with self.assertRaises(ClockMappingError): model.snapshot(80_000_000_000)
        model = LiveClockModel()
        with self.assertRaises(ClockMappingError): model.snapshot(1)
        model.observe(100_000_000_000, 80_000_000_000, 100)
        with self.assertRaises(ClockMappingError): model.snapshot(83_000_000_001)
        with self.assertRaises(ClockMappingError): model.snapshot(79_000_000_000)

    def test_plan_and_session_snapshot_survive_later_clock_updates(self):
        model = LiveClockModel()
        model.observe(100_000_000_000, 80_000_000_000, 100)
        model.observe(101_000_000_000, 81_000_050_000, 100)
        snap = model.snapshot(81_000_050_000)
        saved = snap.apply(session())
        restored = run_session_from_dict(run_session_to_dict(saved))
        self.assertEqual(saved, restored)
        self.assertEqual(saved.playback_scale, 1)
        self.assertNotEqual(saved.clock_scale, 1)
        cross = 101_100_000_000
        window = GateWindow(2, cross, cross-15_000_000, cross, 10)
        decision = SortingDecision('d', 'test', 101_000_000_000, window.open_timestamp_ns,
            (2,), close_timestamp_ns=cross, crossing_timestamp_ns=cross,
            gate_windows=(window,), nozzle_map_sha256='a'*64)
        record = SimpleNamespace(bean_ref=BeanRef('clock-test', 1), decision=decision)
        pending = _pending_actuation(record, saved, 81_000_050_000)
        plan = _external_actuation_plan(pending)
        before = asdict(plan)
        self.assertEqual(plan.run_clock_scale_ppb, snap.source_rate_ppb)
        self.assertEqual(plan.crossing_monotonic_ns, snap.source_to_monotonic_ns(cross))
        model.observe(102_000_000_000, 82_000_070_000, 100)
        newer = model.snapshot(82_000_070_000).apply(saved)
        self.assertNotEqual(saved.clock_monotonic_ns, newer.clock_monotonic_ns)
        self.assertEqual(asdict(_external_actuation_plan(pending)), before)
        self.assertLessEqual(abs(saved.monotonic_to_source_ns(plan.crossing_monotonic_ns)-cross), 1)

    def test_native_event_ingestion_does_not_use_delivery_time(self):
        producer = FastCapLiveProducer(Path('/pack'), Path('/state'), {}, duration_seconds=30)
        producer.process = SimpleNamespace(stdout=io.StringIO(
            '{"event":"clock_sample","counter_timestamp_ns":100000000000,'
            '"counter_monotonic_ns":80000000000,"clock_sample_bracket_ns":32}\n'))
        producer._read_events()
        snap = producer.live_clock.snapshot(80_000_100_000)
        self.assertEqual(snap.source_to_monotonic_ns(100_000_000_000), 80_000_000_000)
        with self.assertRaises(ClockMappingError): producer.clock_snapshot()

    def test_replay_rate_stays_unchanged(self):
        s = session()
        self.assertEqual(s.clock_scale, s.playback_scale)
        self.assertEqual(s.source_to_monotonic_ns(101_000_000_000), 81_000_000_000)


if __name__ == '__main__':
    unittest.main()
