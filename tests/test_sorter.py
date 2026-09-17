import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_registry import track

from beanoflight.classification import (
    CLASSIFICATION_DECISION_BASIS,
    CLASSIFICATION_EVIDENCE,
    CLASSIFICATION_POOLED,
    pool_classification_evidence,
)
from beanoflight.classification_transport import (
    DirectEvidenceBatch,
    DirectInferenceEvidence,
)
from beanoflight.models import BeanEvent, BeanRef, Gate, GateProbability, TrackStatus
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import (
    Enrichment,
    InferenceJob,
    InferenceStatus,
    RunSession,
    RunState,
    SortingDecision,
    record_to_dict,
)
from beanoflight.sorter import (
    SorterService,
    SorterSettings,
    _actuation_timing_result,
    _latest_only_context_batches,
    _pending_actuation,
    _PendingRegistryRecovery,
    _select_gate_indices,
)
from beanoflight.sorting_context_transport import SortingContext, SortingContextBatch
from beanoflight.timing_ledger import bean_timing_ledger


class SorterTimingTests(unittest.TestCase):
    def test_performance_service_restores_cyclic_gc_on_close(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
            suppress_cyclic_gc=True,
        )
        with (
            patch("beanoflight.sorter.gc.isenabled", return_value=True),
            patch("beanoflight.sorter.gc.collect") as collect,
            patch("beanoflight.sorter.gc.disable") as disable,
            patch("beanoflight.sorter.gc.enable") as enable,
            patch("beanoflight.sorter.threading.Thread.start"),
            patch("beanoflight.sorter.threading.Thread.join"),
        ):
            service.start()
            service.close()

        self.assertEqual(collect.call_args_list[0].args, ())
        self.assertEqual(collect.call_args_list[-1].args, (2,))
        self.assertEqual(collect.call_count, 2)
        disable.assert_called_once_with()
        enable.assert_called_once_with()

    def test_managed_gc_requires_queue_and_deadline_slack(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
            suppress_cyclic_gc=True,
        )
        now_ns = time.monotonic_ns()
        service._gc_last_activity_ns = now_ns - 1_000_000_000
        self.assertTrue(service._gc_window_is_safe(now_ns, generation=0))

        service._direct_ingress.put_nowait(object())
        self.assertFalse(service._gc_window_is_safe(now_ns, generation=0))
        service._direct_ingress.get_nowait()
        service._direct_ingress.task_done()

        service._pending_registry_recovery[BeanRef("gc-run", 1)] = (
            _PendingRegistryRecovery(
                record=None,  # type: ignore[arg-type] - deadline-only fixture
                due_monotonic_ns=now_ns + 5_000_000,
            )
        )
        self.assertFalse(service._gc_window_is_safe(now_ns, generation=0))

    def test_gc_pressure_requests_feeder_slowdown_until_full_collection(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
            suppress_cyclic_gc=True,
        )
        service._gc_last_pressure_warning_ns = -1_000_000_000_000
        with patch("builtins.print") as warning:
            service._raise_gc_pressure_warning(time.monotonic_ns(), 72.0)

        self.assertTrue(service.gc_pressure_active)
        self.assertEqual(service.gc_pressure_warnings, 1)
        warning.assert_called_once()
        with (
            patch("beanoflight.sorter.gc.collect", return_value=0),
            patch("beanoflight.sorter.current_rss_mib", return_value=100.0),
        ):
            service._run_gc_collection(2)
        self.assertFalse(service.gc_pressure_active)

    def test_full_collection_requires_a_one_second_deadline_gap(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
            suppress_cyclic_gc=True,
        )
        now_ns = time.monotonic_ns()
        service._gc_last_activity_ns = now_ns - 1_000_000_000
        service._pending_registry_recovery[BeanRef("full-gc-run", 1)] = (
            _PendingRegistryRecovery(
                record=None,  # type: ignore[arg-type] - deadline-only fixture
                due_monotonic_ns=now_ns + 500_000_000,
            )
        )

        self.assertTrue(service._gc_window_is_safe(now_ns, generation=0))
        self.assertFalse(service._gc_window_is_safe(now_ns, generation=2))

    def test_full_collection_requires_a_real_ingress_lull(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
            suppress_cyclic_gc=True,
        )
        now_ns = time.monotonic_ns()
        service._gc_last_activity_ns = now_ns - 100_000_000

        self.assertTrue(service._gc_window_is_safe(now_ns, generation=0))
        self.assertFalse(service._gc_window_is_safe(now_ns, generation=2))

    def test_long_lived_deduplication_caches_are_bounded(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
        )
        with (
            patch("beanoflight.sorter.PLANNED_BEAN_CACHE_CAPACITY", 2),
            patch("beanoflight.sorter.EXTERNAL_DECISION_CACHE_CAPACITY", 2),
        ):
            for sequence in range(1, 4):
                service._remember_planned(BeanRef("bounded-run", sequence))
                service._remember_external_decision(f"decision-{sequence}")

        self.assertNotIn(BeanRef("bounded-run", 1), service._planned)
        self.assertEqual(len(service._planned), 2)
        self.assertNotIn("decision-1", service._externally_scheduled)
        self.assertEqual(len(service._externally_scheduled), 2)

    def test_context_burst_retains_only_latest_item_per_bean(self):
        first_ref = BeanRef("context-coalesce-run", 1)
        second_ref = BeanRef("context-coalesce-run", 2)
        first_old = track(first_ref, 0, 100, -25.0)
        first_new = track(first_ref, 1, 200, -10.0)
        second = track(second_ref, 0, 100, -25.0)
        batches = (
            SortingContextBatch(
                first_ref.run_id,
                0,
                60.0,
                60.0,
                0,
                1_000,
                2,
                2_000,
                (SortingContext(first_old, None), SortingContext(second, None)),
            ),
            SortingContextBatch(
                first_ref.run_id,
                1,
                60.0,
                60.0,
                0,
                1_000,
                2,
                3_000,
                (SortingContext(first_new, None),),
            ),
        )

        coalesced = _latest_only_context_batches(batches)

        self.assertEqual(len(coalesced), 2)
        self.assertEqual(
            tuple(item.track.bean_ref for item in coalesced[0].items),
            (second_ref,),
        )
        self.assertEqual(coalesced[1].items[0].track, first_new)

    def test_direct_evidence_can_decide_from_its_embedded_context(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("embedded-context-run", 1)
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
                {"crops_per_bean": 1},
            )
        )
        snapshot = track(bean_ref, 0, 1_000_000, -25.0)
        prediction = TrajectoryPredictor(GateLayout(60.0)).predict(snapshot)
        registry.update_track(snapshot, prediction)
        job = InferenceJob(
            "embedded-job-1",
            bean_ref,
            InferenceStatus.COMPLETED,
            "CamL",
            0,
            snapshot.timestamp_ns,
            1,
            224,
            224,
            False,
            snapshot.timestamp_ns,
            snapshot.timestamp_ns,
        )
        evidence = Enrichment(
            "mock-inferencer",
            CLASSIFICATION_EVIDENCE,
            {
                "category": "acceptable",
                "class_order": ["acceptable", "mould"],
                "probabilities": [0.9, 0.1],
                "ensemble": {
                    "id": "embedded-context-run:1:model",
                    "sample_index": 1,
                    "expected_samples": 1,
                },
            },
            snapshot.timestamp_ns,
            result_id=job.job_id,
            confidence=0.9,
        )
        sorter = SorterService()

        sorter._process_direct_evidence(
            DirectEvidenceBatch(
                "embedded-batch",
                anchor_ns + 1,
                (
                    DirectInferenceEvidence(
                        job,
                        evidence,
                        SortingContext(snapshot, prediction),
                    ),
                ),
            ),
            registry,
            received_monotonic_ns=anchor_ns + 2,
        )

        decided = registry.get(bean_ref)
        self.assertIsNotNone(decided.decision)
        marks = decided.decision.timing_marks_ns
        self.assertEqual(marks["sorting_context_embedded_with_evidence"], 1)
        self.assertEqual(marks["classification_sample_count"], 1)

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

    def test_registry_recovery_refreshes_new_direct_evidence(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("recovery-refresh-run", 1)
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
                    "id": "recovery-refresh-run:1:model",
                    "sample_index": 1,
                    "expected_samples": 2,
                },
            },
            110,
            result_id="refresh-job-1",
            confidence=0.8,
        )
        second = replace(
            first,
            value={
                **first.value,
                "ensemble": {
                    **first.value["ensemble"],
                    "sample_index": 2,
                },
            },
            timestamp_ns=120,
            result_id="refresh-job-2",
        )
        record = registry.add_enrichment(bean_ref, first)
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
                    "complete:refresh-job-1",
                    1,
                ),
            ),
            registry,
            received_monotonic_ns=received_ns,
            use_embedded_state=True,
            defer_classifications=True,
        )
        sorter._direct_evidence[bean_ref] = {second.result_id: second}
        sorter._direct_timing[second.result_id] = (
            received_ns + 1_000,
            received_ns + 2_000,
        )

        sorter._release_due_registry_recoveries(registry)

        decided = registry.get(bean_ref)
        basis = next(
            item
            for item in decided.enrichments
            if item.kind == CLASSIFICATION_DECISION_BASIS
        )
        self.assertEqual(basis.value["ensemble"]["sample_count"], 2)
        marks = decided.decision.timing_marks_ns
        self.assertEqual(marks["classification_direct_path"], 1)
        self.assertEqual(marks["registry_recovery_evidence_refreshed"], 1)

    def test_pending_ensemble_accepts_direct_evidence_without_new_context(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("pending-refresh-run", 1)
        now_ns = time.monotonic_ns()
        registry.put_session(
            RunSession(
                bean_ref.run_id,
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
        first = Enrichment(
            "mock-inferencer",
            CLASSIFICATION_EVIDENCE,
            {
                "category": "acceptable",
                "class_order": ["acceptable", "mould"],
                "probabilities": [0.8, 0.2],
                "ensemble": {
                    "id": "pending-refresh-run:1:model",
                    "sample_index": 1,
                    "expected_samples": 2,
                },
            },
            110,
            result_id="pending-job-1",
            confidence=0.8,
        )
        second = replace(
            first,
            value={
                **first.value,
                "ensemble": {
                    **first.value["ensemble"],
                    "sample_index": 2,
                },
            },
            timestamp_ns=120,
            result_id="pending-job-2",
        )
        sorter = SorterService()
        sorter._consider(
            replace(record, enrichments=(first,)),
            registry,
            arrival_monotonic_ns=now_ns,
            direct_path=True,
            direct_sent_monotonic_ns=now_ns,
            direct_received_monotonic_ns=now_ns,
        )
        self.assertIn(bean_ref, sorter._awaiting_ensemble)
        job = InferenceJob(
            second.result_id,
            bean_ref,
            InferenceStatus.COMPLETED,
            "CamL",
            2,
            120,
            record.revision,
            224,
            224,
            False,
            120,
            120,
        )

        sorter._process_direct_evidence(
            DirectEvidenceBatch(
                "pending-batch",
                now_ns + 1,
                (DirectInferenceEvidence(job, second),),
            ),
            registry,
            received_monotonic_ns=now_ns + 2,
        )

        decided = registry.get(bean_ref)
        basis = next(
            item
            for item in decided.enrichments
            if item.kind == CLASSIFICATION_DECISION_BASIS
        )
        self.assertEqual(basis.value["ensemble"]["sample_count"], 2)

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
