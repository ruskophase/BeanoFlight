import time
import unittest
from dataclasses import replace

from test_registry import track

from beanoflight.classification import (
    CLASSIFICATION_DECISION_BASIS,
    CLASSIFICATION_EVIDENCE,
    CLASSIFICATION_POOLED,
    pool_classification_evidence,
)
from beanoflight.models import BeanEvent, BeanRef, Gate, GateProbability, TrackStatus
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import (
    Enrichment,
    RunSession,
    RunState,
    SortingDecision,
    record_to_dict,
)
from beanoflight.sorter import (
    SorterService,
    SorterSettings,
    _actuation_timing_result,
    _pending_actuation,
    _select_gate_indices,
)
from beanoflight.sorting_context_transport import SortingContext, SortingContextBatch
from beanoflight.timing_ledger import bean_timing_ledger


class SorterTimingTests(unittest.TestCase):
    def test_new_trajectory_recalculates_pending_pool_deadline(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("trajectory-deadline-run", 1)
        anchor_ns = time.monotonic_ns()
        registry.put_session(
            RunSession(
                bean_ref.run_id,
                0,
                RunState.RUNNING,
                "/synthetic",
                "raw",
                100,
                60.0,
                60.0,
                0,
                0,
                anchor_ns,
                False,
                1,
                1,
                {"crops_per_bean": 2},
            )
        )
        snapshot = track(bean_ref, 0, 1_000_000, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        record = registry.update_track(snapshot, prediction)
        evidence = Enrichment(
            "mock-inferencer",
            CLASSIFICATION_EVIDENCE,
            {
                "category": "mould",
                "class_order": ["acceptable", "mould"],
                "probabilities": [0.1, 0.9],
                "ensemble": {
                    "id": "trajectory-deadline-run:1:model",
                    "sample_index": 1,
                    "expected_samples": 2,
                },
            },
            snapshot.timestamp_ns,
            result_id="trajectory-job-1",
            confidence=0.9,
        )
        sorter = SorterService()
        sorter._consider(
            replace(record, enrichments=(evidence,)),
            registry,
            direct_path=True,
        )
        original_deadline = sorter._awaiting_ensemble[bean_ref].deadline_monotonic_ns
        revised = replace(
            prediction,
            crossing_timestamp_ns=prediction.crossing_timestamp_ns - 20_000_000,
        )
        batch = SortingContextBatch(
            bean_ref.run_id,
            1,
            60.0,
            60.0,
            0,
            anchor_ns,
            2,
            time.monotonic_ns(),
            (SortingContext(snapshot, revised),),
        )

        sorter._process_sorting_context(
            batch,
            registry=None,
            received_monotonic_ns=time.monotonic_ns(),
        )

        revised_deadline = sorter._awaiting_ensemble[bean_ref].deadline_monotonic_ns
        self.assertEqual(original_deadline - revised_deadline, 20_000_000)

    def test_sorter_rejects_changed_clock_anchor_within_epoch(self):
        bean_ref = BeanRef("clock-context-run", 1)
        snapshot = track(bean_ref, 0, 100, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        anchor_ns = time.monotonic_ns() + 100_000_000
        batch = SortingContextBatch(
            bean_ref.run_id,
            0,
            60.0,
            60.0,
            100,
            anchor_ns,
            2,
            time.monotonic_ns(),
            (SortingContext(snapshot, prediction),),
        )
        sorter = SorterService()

        sorter._process_sorting_context(
            batch,
            registry=None,
            received_monotonic_ns=time.monotonic_ns(),
        )
        sorter._process_sorting_context(
            replace(batch, clock_monotonic_ns=anchor_ns + 1),
            registry=None,
            received_monotonic_ns=time.monotonic_ns(),
        )

        self.assertEqual(sorter.clock_anchor_mismatches, 1)
        self.assertEqual(sorter.errors, 1)
        self.assertEqual(
            sorter._sessions[bean_ref.run_id].clock_monotonic_ns,
            anchor_ns,
        )

    def test_registry_notification_recovers_a_missing_direct_message(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("recovery-run", 1)
        snapshot = track(bean_ref, 0, 100, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        record = registry.update_track(snapshot, prediction)
        record = registry.add_enrichment(
            bean_ref,
            Enrichment(
                "mock-inferencer",
                "classification",
                {"category": "acceptable"},
                110,
                confidence=0.95,
            ),
        )
        sorter = SorterService()
        received_ns = time.monotonic_ns() - 6_000_000

        sorter._process_events(
            (
                BeanEvent(
                    "inference.completed",
                    bean_ref,
                    110,
                    {"record": record_to_dict(record, include_history=False)},
                    record.revision,
                    "complete:job-1",
                    1,
                ),
            ),
            registry,
            received_monotonic_ns=received_ns,
            use_embedded_state=True,
            defer_classifications=True,
        )
        self.assertIsNone(registry.get(bean_ref).decision)

        sorter._release_due_registry_recoveries(registry)

        decided = registry.get(bean_ref)
        self.assertIsNotNone(decided.decision)
        self.assertEqual(sorter.registry_recovery_decisions, 1)
        self.assertEqual(
            decided.decision.timing_marks_ns["classification_direct_path"], 0
        )

    def test_deadline_fallback_finalizes_first_probability_vector(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("fallback-run", 1)
        now_ns = time.monotonic_ns()
        registry.put_session(
            RunSession(
                "fallback-run",
                0,
                RunState.RUNNING,
                "/synthetic",
                "raw",
                10,
                60.0,
                60.0,
                100,
                100,
                now_ns,
                False,
                1,
                1,
                {"crops_per_bean": 2},
            )
        )
        snapshot = track(bean_ref, 0, 100, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        record = registry.update_track(snapshot, prediction)
        record = registry.add_enrichment(
            bean_ref,
            Enrichment(
                "mock-inferencer",
                CLASSIFICATION_EVIDENCE,
                {
                    "category": "acceptable",
                    "class_order": [
                        "acceptable",
                        "insect_damage",
                        "mould",
                        "broken",
                    ],
                    "probabilities": [0.8, 0.1, 0.05, 0.05],
                    "ensemble": {
                        "id": "fallback-run:1:model",
                        "sample_index": 1,
                        "expected_samples": 2,
                    },
                },
                110,
                result_id="job-1",
                confidence=0.8,
            ),
        )
        sorter = SorterService()

        sorter._consider(
            record,
            registry,
            arrival_monotonic_ns=now_ns,
            force_deadline_fallback=True,
        )

        decided = registry.get(bean_ref)
        pooled = next(
            item
            for item in decided.enrichments
            if item.kind == CLASSIFICATION_POOLED
        )
        self.assertTrue(pooled.value["ensemble"]["deadline_fallback"])
        self.assertEqual(pooled.value["ensemble"]["sample_count"], 1)
        self.assertIn("deadline fallback 1/2", decided.decision.reason)
        self.assertEqual(
            decided.decision.timing_marks_ns["classification_deadline_fallback"],
            1,
        )
        self.assertEqual(sorter.deadline_fallbacks, 1)

    def test_decision_basis_survives_complete_pool_deadline_race(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("pool-race-run", 1)
        now_ns = time.monotonic_ns()
        registry.put_session(
            RunSession(
                "pool-race-run",
                0,
                RunState.RUNNING,
                "/synthetic",
                "raw",
                10,
                60.0,
                60.0,
                100,
                100,
                now_ns,
                False,
                1,
                1,
                {"crops_per_bean": 2},
            )
        )
        snapshot = track(bean_ref, 0, 100, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        registry.update_track(snapshot, prediction)
        first = Enrichment(
            "mock-inferencer",
            CLASSIFICATION_EVIDENCE,
            {
                "category": "acceptable",
                "class_order": ["acceptable", "mould"],
                "probabilities": [0.8, 0.2],
                "ensemble": {
                    "id": "pool-race-run:1:model",
                    "sample_index": 1,
                    "expected_samples": 2,
                },
            },
            110,
            result_id="race-job-1",
            confidence=0.8,
        )
        second = replace(
            first,
            value={
                **first.value,
                "probabilities": [0.2, 0.8],
                "category": "mould",
                "ensemble": {
                    **first.value["ensemble"],
                    "sample_index": 2,
                },
            },
            timestamp_ns=120,
            result_id="race-job-2",
        )
        first_only = registry.add_enrichment(bean_ref, first)
        registry.add_enrichment(bean_ref, second)
        full_pool = pool_classification_evidence(
            (first, second), deadline_fallback=False
        )
        registry.add_enrichment(bean_ref, full_pool)

        SorterService()._consider(
            first_only,
            registry,
            arrival_monotonic_ns=now_ns,
            force_deadline_fallback=True,
            direct_path=True,
        )

        decided = registry.get(bean_ref)
        canonical = next(
            item
            for item in decided.enrichments
            if item.kind == CLASSIFICATION_POOLED
        )
        basis = next(
            item
            for item in decided.enrichments
            if item.kind == CLASSIFICATION_DECISION_BASIS
        )
        self.assertFalse(canonical.value["ensemble"]["deadline_fallback"])
        self.assertEqual(canonical.value["ensemble"]["sample_count"], 2)
        self.assertTrue(basis.value["ensemble"]["deadline_fallback"])
        self.assertEqual(basis.value["ensemble"]["sample_count"], 1)
        self.assertEqual(
            decided.decision.timing_marks_ns["classification_deadline_fallback"],
            1,
        )
        ledger = bean_timing_ledger(decided)
        self.assertEqual(
            ledger["classification"]["kind"], CLASSIFICATION_DECISION_BASIS
        )
        self.assertTrue(ledger["classification"]["deadline_fallback"])

    def test_direct_deadline_waits_for_post_drain_finalization(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("drain-run", 1)
        now_ns = time.monotonic_ns()
        registry.put_session(
            RunSession(
                "drain-run",
                0,
                RunState.RUNNING,
                "/synthetic",
                "raw",
                10,
                60.0,
                60.0,
                100,
                100,
                now_ns - 1_000_000_000,
                False,
                1,
                1,
                {"crops_per_bean": 2},
            )
        )
        snapshot = track(bean_ref, 0, 100, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        record = registry.update_track(snapshot, prediction)
        first = Enrichment(
            "mock-inferencer",
            CLASSIFICATION_EVIDENCE,
            {
                "category": "acceptable",
                "class_order": ["acceptable", "mould"],
                "probabilities": [0.8, 0.2],
                "ensemble": {
                    "id": "drain-run:1:model",
                    "sample_index": 1,
                    "expected_samples": 2,
                },
            },
            110,
            result_id="drain-job-1",
            confidence=0.8,
        )
        second = replace(
            first,
            value={
                **first.value,
                "probabilities": [0.2, 0.8],
                "category": "mould",
                "ensemble": {
                    **first.value["ensemble"],
                    "sample_index": 2,
                },
            },
            timestamp_ns=120,
            result_id="drain-job-2",
        )
        sorter = SorterService()
        first_only = replace(record, enrichments=(first,))

        sorter._consider(
            first_only,
            registry,
            arrival_monotonic_ns=now_ns,
            direct_path=True,
        )

        self.assertIsNone(registry.get(bean_ref).decision)
        self.assertIn(bean_ref, sorter._awaiting_ensemble)

        sorter._consider(
            replace(record, enrichments=(first, second)),
            registry,
            arrival_monotonic_ns=now_ns + 1,
            direct_path=True,
        )

        decided = registry.get(bean_ref)
        basis = next(
            item
            for item in decided.enrichments
            if item.kind == CLASSIFICATION_DECISION_BASIS
        )
        self.assertFalse(basis.value["ensemble"]["deadline_fallback"])
        self.assertEqual(basis.value["ensemble"]["sample_count"], 2)

    def test_low_confidence_reject_is_accounted_separately(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("confidence-run", 1)
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
                confidence=0.72,
            ),
        )
        sorter = SorterService()

        sorter._consider(record, registry)

        decided = registry.get(bean_ref)
        self.assertIn("below confidence threshold", decided.decision.reason)
        self.assertEqual(sorter.low_confidence_defects, 1)
        self.assertEqual(
            bean_timing_ledger(decided)["result"], "low_confidence_defect"
        )

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
