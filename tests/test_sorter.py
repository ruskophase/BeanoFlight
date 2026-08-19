import time
import unittest
from dataclasses import replace

from test_registry import track

from beanoflight.models import BeanRef, Gate, GateProbability, TrackStatus
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import (
    Enrichment,
    RunSession,
    RunState,
    SortingDecision,
)
from beanoflight.sorter import (
    SorterService,
    SorterSettings,
    _actuation_timing_result,
    _pending_actuation,
    _select_gate_indices,
)


class SorterTimingTests(unittest.TestCase):
    def test_adjacent_gate_probabilities_can_jointly_qualify(self):
        gates = (
            GateProbability(Gate(-1, -7.5, -2.5), 0.20),
            GateProbability(Gate(0, -2.5, 2.5), 0.25),
            GateProbability(Gate(1, 2.5, 7.5), 0.10),
        )

        selected, combined = _select_gate_indices(
            gates, 0.35, allow_adjacent_pair=True
        )

        self.assertEqual(selected, (-1, 0))
        self.assertAlmostEqual(combined, 0.45)
        self.assertEqual(
            _select_gate_indices(gates, 0.35, allow_adjacent_pair=False),
            ((), None),
        )
    def test_pending_actuation_converts_source_deadlines_once(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("clock-run", 1)
        record = registry.update_track(track(bean_ref, 0, 1_000, -20.0))
        decision = SortingDecision(
            "decision-1",
            "sorter",
            1_500,
            2_000,
            (0,),
            close_timestamp_ns=4_000,
            crossing_timestamp_ns=3_000,
        )
        record = replace(record, decision=decision)
        session = RunSession(
            "clock-run",
            1,
            RunState.RUNNING,
            "/synthetic",
            "raw",
            10,
            60.0,
            60.0,
            0,
            1_000,
            10_000,
            False,
            1,
            2,
            {},
        )

        pending = _pending_actuation(record, session, 99_000)

        self.assertEqual(pending.open_monotonic_ns, 11_000)
        self.assertEqual(pending.close_monotonic_ns, 13_000)

    def test_actuation_succeeds_only_when_gate_spans_crossing(self):
        decision = SortingDecision(
            "decision-1",
            "sorter",
            1_500,
            2_000,
            (0,),
            close_timestamp_ns=4_000,
            crossing_timestamp_ns=3_000,
        )

        success, detail = _actuation_timing_result(decision, 2_500, 3_500)
        self.assertTrue(success)
        self.assertIn("active", detail)

        success, detail = _actuation_timing_result(decision, 3_100, 4_000)
        self.assertFalse(success)
        self.assertIn("after crossing", detail)

    def test_cancelled_tentative_track_gets_no_action_decision(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("cancelled-run", 1)
        record = registry.update_track(
            track(bean_ref, 0, 100, -20.0, status=TrackStatus.CANCELLED)
        )
        record = registry.add_enrichment(
            bean_ref,
            Enrichment(
                "mock-inferencer",
                "classification",
                {"category": "mould"},
                110,
                confidence=0.95,
            ),
        )

        SorterService()._consider(record, registry)

        decided = registry.get(bean_ref)
        self.assertEqual(decided.decision.gate_indices, ())
        self.assertIn("cancelled", decided.decision.reason)
        self.assertIsNotNone(decided.decision.acknowledged_timestamp_ns)

    def test_scheduler_arrival_time_can_reject_an_already_late_plan(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("late-run", 1)
        registry.put_session(
            RunSession(
                "late-run",
                0,
                RunState.RUNNING,
                "/synthetic",
                "raw",
                10,
                60.0,
                60.0,
                100,
                100,
                time.monotonic_ns() - 1_000_000_000,
                False,
                1,
                1,
                {},
            )
        )
        snapshot = track(bean_ref, 0, 100, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        record = registry.update_track(snapshot, prediction)
        record = registry.add_enrichment(
            bean_ref,
            Enrichment(
                "mock-inferencer",
                "classification",
                {"category": "mould"},
                110,
                confidence=0.95,
            ),
        )

        SorterService(
            settings=SorterSettings(gate_probability_threshold=0.05)
        )._consider(record, registry)

        decided = registry.get(bean_ref)
        self.assertEqual(decided.decision.gate_indices, ())
        self.assertIn("too late", decided.decision.reason)


if __name__ == "__main__":
    unittest.main()
