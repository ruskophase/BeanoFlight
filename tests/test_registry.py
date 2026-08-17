import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from beanoflight.models import (
    BeanRef,
    Detection,
    Observation,
    TrackSnapshot,
    TrackStatus,
)
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.registry import (
    BeanNotFoundError,
    BeanRegistry,
    RegistryConflictError,
    StaleRegistryUpdateError,
)
from beanoflight.registry_models import (
    ActuationResult,
    Enrichment,
    InferenceJob,
    InferenceStatus,
    RunSession,
    RunState,
    SortingDecision,
)
from beanoflight.registry_sqlite import SQLiteBeanRepository


def observation(frame_index: int, timestamp_ns: int, y_mm: float) -> Observation:
    return Observation(
        frame_index=frame_index,
        timestamp_ns=timestamp_ns,
        detection=Detection(
            centroid_px=(200.0, 50.0 + frame_index * 40.0),
            bbox_px=(170, 20 + frame_index * 40, 60, 55),
            area_px=2_500,
            solidity=0.91,
            mean_bgr=(40.0, 80.0, 120.0),
        ),
        position_mm=(0.5, y_mm),
    )


def track(
    bean_ref: BeanRef,
    frame_index: int,
    timestamp_ns: int,
    y_mm: float,
    *,
    status: TrackStatus = TrackStatus.CONFIRMED,
) -> TrackSnapshot:
    item = observation(frame_index, timestamp_ns, y_mm)
    covariance = tuple(
        tuple(0.04 if row == column else 0.0 for column in range(4)) for row in range(4)
    )
    return TrackSnapshot(
        bean_ref=bean_ref,
        status=status,
        timestamp_ns=timestamp_ns,
        state=(0.5, y_mm, 4.0, 900.0),
        covariance=covariance,
        hits=frame_index + 2,
        misses=0,
        last_bbox_px=item.detection.bbox_px,
        history=(item,),
    )


class BeanRegistryTests(unittest.TestCase):
    def test_run_clock_inference_job_and_actuation_contract(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("simulation-run", 1)
        session = registry.put_session(
            RunSession(
                "simulation-run",
                0,
                RunState.CREATED,
                "/recording",
                "mkv",
                100,
                60.0,
                30.0,
                1_000,
                1_000,
                10_000,
                False,
                20,
                20,
                {"seed": 7},
            ),
            expected_revision=0,
        )
        session = registry.put_session(
            replace(session, state=RunState.RUNNING, updated_timestamp_ns=21),
            expected_revision=1,
        )
        self.assertEqual(session.source_to_monotonic_ns(2_000), 12_000)
        self.assertEqual(session.monotonic_to_source_ns(12_000), 2_000)
        paused = replace(
            session,
            state=RunState.PAUSED,
            clock_source_timestamp_ns=2_000,
            clock_monotonic_ns=12_000,
        )
        self.assertEqual(paused.monotonic_to_source_ns(99_000), 2_000)
        with self.assertRaises(RegistryConflictError):
            registry.put_session(
                replace(session, source_path="/different-recording"),
                expected_revision=session.revision,
            )

        registry.update_track(track(bean_ref, 0, 100, -25.0), event_id="track")
        job = InferenceJob(
            "job-1",
            bean_ref,
            InferenceStatus.SUBMITTED,
            "CamL",
            0,
            100,
            1,
            300,
            300,
            False,
            100,
            100,
        )
        registry.submit_inference_job(job)
        registry.update_inference_job(
            bean_ref,
            job.job_id,
            InferenceStatus.ACCEPTED,
            105,
            event_id="accept-job",
        )
        with self.assertRaises(StaleRegistryUpdateError):
            registry.complete_inference_job(
                bean_ref,
                job.job_id,
                Enrichment(
                    "mock-inferencer",
                    "classification",
                    {"category": "mould"},
                    104,
                    result_id="too-early",
                ),
            )
        completed = registry.complete_inference_job(
            bean_ref,
            job.job_id,
            Enrichment(
                "mock-inferencer",
                "classification",
                {"category": "mould"},
                120,
                "mock-v1",
                job.job_id,
                0.91,
            ),
        )
        self.assertEqual(completed.inference_jobs[0].status, InferenceStatus.COMPLETED)
        self.assertEqual(completed.enrichments[0].result_id, job.job_id)

        decision = SortingDecision(
            "decision-1",
            "sorter",
            125,
            180,
            (0,),
            close_timestamp_ns=190,
            crossing_timestamp_ns=185,
            based_on_revision=completed.revision,
        )
        registry.set_sorting_decision(bean_ref, decision)
        actuated = registry.record_actuation(
            bean_ref,
            ActuationResult("decision-1", "virtual-actuator", 181, 191, True),
        )
        self.assertTrue(actuated.actuation.success)

    def test_revisioned_lifecycle_enrichment_and_sorting_decision(self):
        registry = BeanRegistry()
        events = registry.subscribe()
        bean_ref = BeanRef("full-run-uuid", 7)
        first_track = track(bean_ref, 0, 100, -25.0)
        first_prediction = TrajectoryPredictor(GateLayout(60.0)).predict(first_track)

        created = registry.update_track(
            first_track, first_prediction, event_id="track-0"
        )
        duplicate = registry.update_track(
            first_track, first_prediction, event_id="track-0"
        )
        self.assertEqual(created.revision, 1)
        self.assertEqual(duplicate, created)
        self.assertEqual(events.get_nowait().kind, "bean.created")
        self.assertTrue(events.empty())

        second_track = track(bean_ref, 1, 116_666_667, -10.0)
        updated = registry.update_track(second_track, event_id="track-1")
        self.assertEqual(updated.revision, 2)
        self.assertEqual(len(updated.track.history), 2)

        result = Enrichment(
            source="resnet",
            kind="defect",
            value={"category": "weevil"},
            timestamp_ns=120_000_000,
            version="model-v3",
            result_id="result-1",
            confidence=0.94,
        )
        enriched = registry.add_enrichment(bean_ref, result)
        self.assertEqual(enriched.revision, 3)
        self.assertEqual(registry.add_enrichment(bean_ref, result), enriched)
        with self.assertRaises(RegistryConflictError):
            registry.add_enrichment(
                bean_ref, replace(result, value={"category": "mould"})
            )

        decision = SortingDecision(
            decision_id="decision-1",
            source="sorter",
            timestamp_ns=125_000_000,
            actuation_timestamp_ns=180_000_000,
            gate_indices=(0, 1),
            policy_version="policy-v1",
            reason="combined gate probability",
        )
        decided = registry.set_sorting_decision(bean_ref, decision)
        acknowledged = registry.acknowledge_sorting_decision(
            bean_ref, decision.decision_id, 181_000_000, event_id="ack-1"
        )
        self.assertEqual(decided.revision, 4)
        self.assertEqual(acknowledged.revision, 5)
        self.assertEqual(acknowledged.decision.acknowledged_timestamp_ns, 181_000_000)
        journal = registry.events_since(0)
        self.assertEqual([event.stream_sequence for event in journal], [1, 2, 3, 4, 5])
        self.assertEqual(registry.events_since(3), journal[3:])
        with self.assertRaises(RegistryConflictError):
            registry.set_sorting_decision(
                bean_ref, replace(decision, decision_id="decision-2")
            )

    def test_rejects_stale_track_and_reused_event_id(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("run", 1)
        current = track(bean_ref, 1, 200, -10.0)
        registry.update_track(current, event_id="track-event")
        with self.assertRaises(StaleRegistryUpdateError):
            registry.update_track(track(bean_ref, 0, 100, -20.0))
        with self.assertRaises(RegistryConflictError):
            registry.update_track(track(bean_ref, 2, 300, 0.0), event_id="track-event")
        with self.assertRaises(RegistryConflictError):
            registry.update_track(track(BeanRef("run", -1), 0, 100, -20.0))

    def test_terminal_track_cannot_be_resurrected(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("run", 1)
        registry.update_track(track(bean_ref, 0, 100, -20.0))
        registry.update_track(track(bean_ref, 1, 200, 40.0, status=TrackStatus.EXITED))
        with self.assertRaises(RegistryConflictError):
            registry.update_track(track(bean_ref, 2, 300, 50.0))

    def test_only_evicts_terminal_records_after_decision_acknowledgement(self):
        registry = BeanRegistry()
        bean_ref = BeanRef("run", 1)
        exited = track(bean_ref, 2, 300, 40.0, status=TrackStatus.EXITED)
        registry.update_track(exited)
        self.assertEqual(registry.evict_completed(before_timestamp_ns=301), 1)


class SQLiteRegistryTests(unittest.TestCase):
    def test_track_persistence_does_not_replace_session_wall_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "beanoflight.db"
            wall_clock_ns = 1_786_960_000_000_000_000
            source_clock_ns = 235_228_702_332_000
            session = RunSession(
                "clock-domain-run",
                0,
                RunState.RUNNING,
                "/recording",
                "raw-mmap-green",
                601,
                60.0,
                60.0,
                source_clock_ns,
                source_clock_ns,
                1_000,
                False,
                wall_clock_ns,
                wall_clock_ns,
                {},
            )
            with SQLiteBeanRepository(path) as repository:
                registry = BeanRegistry(repository)
                registry.put_session(session)
                registry.update_track(
                    track(BeanRef(session.run_id, 1), 0, source_clock_ns, -25.0)
                )
                restored = repository.load_session(session.run_id)
                self.assertEqual(restored.created_timestamp_ns, wall_clock_ns)

    def test_schema_one_session_is_migrated_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "version-one.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE sessions("
                    "run_id TEXT PRIMARY KEY, created_timestamp_ns INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO sessions VALUES ('old-run', 42)")
                connection.execute("PRAGMA user_version=1")
            with SQLiteBeanRepository(path) as repository:
                session = repository.load_session("old-run")
                self.assertEqual(session.run_id, "old-run")
                self.assertEqual(session.source_kind, "implicit")
                self.assertEqual(session.created_timestamp_ns, 42)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0], 2
                    )

    def test_session_and_async_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "beanoflight.db"
            session = RunSession(
                "run-session",
                0,
                RunState.RUNNING,
                "/recording",
                "raw-bundle",
                601,
                60.0,
                60.0,
                100,
                100,
                1_000,
                False,
                10,
                11,
                {"crop_size_px": 300},
            )
            with SQLiteBeanRepository(path) as repository:
                registry = BeanRegistry(repository)
                expected = registry.put_session(session)
                bean_ref = BeanRef(session.run_id, 1)
                registry.update_track(track(bean_ref, 0, 100, -25.0))
                job = InferenceJob(
                    "job",
                    bean_ref,
                    InferenceStatus.SUBMITTED,
                    "CamL",
                    0,
                    100,
                    1,
                    300,
                    300,
                    False,
                    100,
                    100,
                )
                registry.submit_inference_job(job)
                registry.complete_inference_job(
                    bean_ref,
                    job.job_id,
                    Enrichment(
                        "mock", "classification", "broken", 120, result_id="job"
                    ),
                )
                decision = SortingDecision(
                    "decision", "sorter", 125, 180, (0,), close_timestamp_ns=190
                )
                registry.set_sorting_decision(bean_ref, decision)
                expected_record = registry.record_actuation(
                    bean_ref,
                    ActuationResult("decision", "virtual", 181, 191, True),
                )
            with SQLiteBeanRepository(path) as repository:
                reopened = BeanRegistry(repository)
                restored = reopened.get_session(session.run_id)
                self.assertEqual(restored, expected)
                self.assertEqual(repository.list_sessions(), (expected,))
                self.assertEqual(reopened.get(bean_ref), expected_record)

    def test_frame_batch_rolls_back_registry_and_database_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "beanoflight.db"
            with SQLiteBeanRepository(path) as repository:
                registry = BeanRegistry(repository)
                events = registry.subscribe()
                first_ref = BeanRef("run", 1)
                second_ref = BeanRef("run", 2)
                with self.assertRaises(RegistryConflictError):
                    registry.update_tracks(
                        (
                            (track(first_ref, 0, 100, -25.0), None, "same-id"),
                            (track(second_ref, 0, 100, -25.0), None, "same-id"),
                        )
                    )
                self.assertTrue(events.empty())
                self.assertEqual(repository.list_records(), ())
                with self.assertRaises(BeanNotFoundError):
                    registry.get(first_ref)

    def test_wal_round_trip_and_normalized_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "beanoflight.db"
            bean_ref = BeanRef("persistent-run", 3)
            repository = SQLiteBeanRepository(path)
            registry = BeanRegistry(repository)
            first = track(bean_ref, 0, 100, -25.0)
            registry.update_track(first, event_id="track-0")
            second = track(bean_ref, 1, 116_666_667, -10.0)
            prediction = TrajectoryPredictor(GateLayout(60.0)).predict(second)
            registry.update_track(second, prediction, event_id="track-1")
            enrichment = Enrichment(
                "resnet", "defect", "clear", 120_000_000, "v4", "result-1", 0.99
            )
            registry.add_enrichment(bean_ref, enrichment)
            decision = SortingDecision(
                "decision-1", "sorter", 125_000_000, 180_000_000, (0,), "v1"
            )
            registry.set_sorting_decision(bean_ref, decision)
            expected = registry.acknowledge_sorting_decision(
                bean_ref, decision.decision_id, 181_000_000
            )
            self.assertEqual(repository.journal_mode, "wal")
            repository.close()

            with SQLiteBeanRepository(path) as reopened:
                restored = BeanRegistry(reopened).get(bean_ref)
                self.assertEqual(restored, expected)
                self.assertEqual(
                    [event.kind for event in reopened.event_history(bean_ref)],
                    [
                        "bean.created",
                        "track.updated",
                        "enrichment.added",
                        "sorting.decision",
                        "sorting.acknowledged",
                    ],
                )
                self.assertEqual(
                    [event.stream_sequence for event in reopened.events_since(1)],
                    [2, 3, 4, 5],
                )
                self.assertEqual(reopened.event_identity("track-1")[0], bean_ref)
                with sqlite3.connect(path) as connection:
                    observation_count = connection.execute(
                        "SELECT COUNT(*) FROM observations"
                    ).fetchone()[0]
                    state_count = connection.execute(
                        "SELECT COUNT(*) FROM track_states"
                    ).fetchone()[0]
                self.assertEqual(observation_count, 2)
                self.assertEqual(state_count, 2)

    def test_persisted_event_id_stays_idempotent_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "beanoflight.db"
            bean_ref = BeanRef("run", 2)
            current = track(bean_ref, 0, 100, -25.0)
            with SQLiteBeanRepository(path) as repository:
                BeanRegistry(repository).update_track(current, event_id="stable-id")
            with SQLiteBeanRepository(path) as repository:
                registry = BeanRegistry(repository)
                duplicate = registry.update_track(current, event_id="stable-id")
                self.assertEqual(duplicate.revision, 1)
                with self.assertRaises(RegistryConflictError):
                    registry.update_track(
                        track(bean_ref, 1, 200, -10.0), event_id="stable-id"
                    )

    def test_reload_preserves_cleared_prediction_and_enrichment_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "beanoflight.db"
            bean_ref = BeanRef("run", 5)
            first = track(bean_ref, 0, 100, -25.0)
            prediction = TrajectoryPredictor(GateLayout(60.0)).predict(first)
            with SQLiteBeanRepository(path) as repository:
                registry = BeanRegistry(repository)
                registry.update_track(first, prediction, event_id="track-0")
                registry.add_enrichment(
                    bean_ref,
                    Enrichment("worker", "note", "first", 300, result_id="result-1"),
                )
                registry.add_enrichment(
                    bean_ref,
                    Enrichment("worker", "note", "second", 200, result_id="result-2"),
                )
                expected = registry.update_track(
                    track(bean_ref, 1, 400, -10.0), None, event_id="track-1"
                )
                self.assertIsNone(expected.prediction)

            with SQLiteBeanRepository(path) as repository:
                restored = BeanRegistry(repository).get(bean_ref)
                self.assertEqual(restored, expected)
                self.assertEqual(
                    [item.value for item in restored.enrichments],
                    ["first", "second"],
                )


if __name__ == "__main__":
    unittest.main()
