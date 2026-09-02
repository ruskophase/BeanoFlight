import gc
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from beanoflight.crop import CropPayload
from beanoflight.models import BeanRef, FrameAnalysis, TrackSnapshot, TrackStatus
from beanoflight.registry_models import InferenceJob, InferenceStatus, RunState
from beanoflight.replay import (
    MAXIMUM_REPLAY_FRAMES,
    CropDispatcher,
    DecodedFrameBuffer,
    ReplayRunner,
    ReplaySettings,
    _emergency_microbatch_groups,
)
from beanoflight.source import SourceError, SourceMetadata


class FakeSequentialSource:
    source_kind = "test"
    path = Path("/synthetic")

    def __init__(self, frame_count=5):
        self.metadata = SourceMetadata(self.path, 8, 6, frame_count, 60.0, True)
        self.calls = []
        self.released = []

    def timestamp_ns(self, index):
        return index * 10

    def frame(self, index):
        self.calls.append(index)
        return np.full((6, 8, 3), index, dtype=np.uint8)

    def close(self):
        pass

    def release_frame(self, frame):
        self.released.append(int(frame[0, 0, 0]))


class FakeLiveSource(FakeSequentialSource):
    live = True
    source_kind = "test-live"

    def __init__(self, frame_count=3):
        super().__init__(frame_count)
        self.clock_source_timestamp_ns = 1_000_000_000
        self.clock_monotonic_ns = time.monotonic_ns()
        self._timestamps = tuple(
            self.clock_source_timestamp_ns + index * 16_666_667
            for index in range(frame_count)
        )

    def timestamp_ns(self, index):
        return self._timestamps[index]

    def stereo_statistics(self):
        return {"sequence_drops": {"CamL": 0, "CamR": 0}}


class FakeEngine:
    def __init__(self):
        self.tracker = type("Tracker", (), {"run_id": "buffered-run"})()
        self.last_registry_revisions = {}

    def process(self, _frame, index, timestamp):
        return FrameAnalysis(index, timestamp, (), (), (), (), 0.1)


class SlowEngine(FakeEngine):
    def process(self, _frame, index, timestamp):
        time.sleep(0.02)
        return FrameAnalysis(index, timestamp, (), (), (), (), 20.0)


class GcRecordingEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.gc_states = []

    def process(self, _frame, index, timestamp):
        self.gc_states.append(gc.isenabled())
        return super().process(_frame, index, timestamp)


class BoundaryCensoringEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.boundary_timestamps = []
        self.tracker.cancel_active_at_boundary = self._cancel_active_at_boundary

    def _cancel_active_at_boundary(self, timestamp):
        self.boundary_timestamps.append(timestamp)
        return (
            TrackSnapshot(
                BeanRef("buffered-run", 1),
                TrackStatus.CANCELLED,
                timestamp,
                (0.0, 0.0, 0.0, 0.0),
                tuple(tuple(0.0 for _ in range(4)) for _ in range(4)),
                1,
                0,
                (0, 0, 1, 1),
                (),
            ),
        )


class FakeRegistry:
    def __init__(self, source):
        self.source = source
        self.transitions = []
        self.sessions = []

    def put_session(self, session, *, expected_revision):
        self.transitions.append((session.state, len(self.source.calls)))
        stored = replace(session, revision=expected_revision + 1)
        self.sessions.append(stored)
        return stored


class DelayedRunningRegistry(FakeRegistry):
    def __init__(self, source, delay_seconds):
        super().__init__(source)
        self.delay_seconds = delay_seconds
        self.running_returns_ns = []

    def put_session(self, session, *, expected_revision):
        if session.state == RunState.RUNNING and not self.running_returns_ns:
            time.sleep(self.delay_seconds)
        stored = super().put_session(session, expected_revision=expected_revision)
        if session.state == RunState.RUNNING:
            self.running_returns_ns.append(time.monotonic_ns())
        return stored


class SlowStartingDispatcher:
    submitted = 0
    dropped = 0

    def __init__(self):
        self.ready_monotonic_ns = 0
        self.frames = []

    def start(self):
        time.sleep(0.01)
        self.ready_monotonic_ns = time.monotonic_ns()

    def enqueue_frame(self, updates, payloads):
        self.frames.append((updates, payloads))
        return True

    def close(self, *, drain=True):
        return None

    def performance_metrics(self):
        return {}


class ReplayBufferTests(unittest.TestCase):
    def test_deadline_critical_second_samples_form_one_bounded_microbatch(self):
        now_ns = 1_000_000_000
        payloads = []
        for sequence, deadline_offset_ms in (
            (1, 20),
            (2, 20.5),
            (3, 26),
            (4, 27),
            (5, 28),
        ):
            job = InferenceJob(
                f"infer:urgent-run:{sequence}:CamL:10:1",
                BeanRef("urgent-run", sequence),
                InferenceStatus.SUBMITTED,
                "CamL",
                10,
                100,
                1,
                4,
                4,
                False,
                100,
                100,
                timing_marks_ns={
                    "run_clock_source_ns": 0,
                    "run_clock_monotonic_ns": now_ns,
                    "run_clock_scale_ppb": 1_000_000_000,
                    "inference_priority_crossing_source_ns": round(
                        (deadline_offset_ms + 17) * 1_000_000
                    ),
                },
            )
            payloads.append(
                CropPayload(job, np.zeros((4, 4, 3), dtype=np.uint8))
            )

        groups = _emergency_microbatch_groups(
            tuple(payloads),
            now_ns,
            enabled=True,
            window_ms=35,
            decision_safety_reserve_ms=17,
        )

        self.assertEqual(tuple(len(group) for group in groups), (2, 3))
        self.assertEqual(
            [item.job.bean_ref.sequence for item in groups[0]], [1, 2]
        )
        self.assertTrue(
            all(
                item.job.timing_marks_ns["emergency_microbatch"] == 1
                for item in groups[0]
            )
        )
        self.assertEqual(
            groups[1][0].job.timing_marks_ns[
                "emergency_microbatch_remainder"
            ],
            1,
        )

    def test_emergency_microbatch_can_be_disabled_for_ab_testing(self):
        job = InferenceJob(
            "infer:disabled-run:1:CamL:10:1",
            BeanRef("disabled-run", 1),
            InferenceStatus.SUBMITTED,
            "CamL",
            10,
            100,
            1,
            4,
            4,
            False,
            100,
            100,
        )
        payloads = (
            CropPayload(job, np.zeros((4, 4, 3), dtype=np.uint8)),
            CropPayload(
                replace(job, job_id="infer:disabled-run:2:CamL:10:1"),
                np.zeros((4, 4, 3), dtype=np.uint8),
            ),
        )

        groups = _emergency_microbatch_groups(
            payloads,
            1_000_000_000,
            enabled=False,
            window_ms=35,
            decision_safety_reserve_ms=17,
            minimum_frame_beans=2,
        )

        self.assertEqual(groups, (payloads,))

    def test_dispatcher_coalesces_track_only_backlog_through_urgent_crop(self):
        dispatcher = CropDispatcher("inproc://registry", "inproc://crops")
        for frame_index in range(3):
            dispatcher.enqueue_frame(((f"track-{frame_index}",),), ())
        job = InferenceJob(
            "job-1",
            BeanRef("dispatch-run", 1),
            InferenceStatus.SUBMITTED,
            "CamL",
            3,
            100,
            1,
            4,
            4,
            False,
            100,
            100,
        )
        dispatcher.enqueue_frame(
            (("track-3",),),
            (CropPayload(job, np.zeros((4, 4, 3), dtype=np.uint8)),),
        )

        with dispatcher._condition:
            selected = dispatcher._take_dispatch_batch_locked()

        self.assertEqual(len(selected), 4)
        self.assertFalse(dispatcher._items)
        self.assertFalse(any(item.payloads for item in selected[:-1]))
        self.assertEqual(selected[-1].payloads[0].job.job_id, "job-1")

    def test_prebuffers_bounded_frames_and_delivers_in_order(self):
        source = FakeSequentialSource(frame_count=5)
        buffer = DecodedFrameBuffer(source, frame_count=4, capacity=3)
        progress = []
        buffered, elapsed = buffer.prebuffer(
            threading.Event(), lambda count, target: progress.append((count, target))
        )
        self.assertEqual(buffered, 3)
        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(progress[-1], (3, 3))
        for index in range(4):
            frame = buffer.frame(index)
            self.assertEqual(int(frame[0, 0, 0]), index)
        buffer.close()
        self.assertEqual(source.calls, [0, 1, 2, 3])

    def test_buffer_rejects_non_sequential_access(self):
        source = FakeSequentialSource(frame_count=2)
        buffer = DecodedFrameBuffer(source, frame_count=2, capacity=1)
        buffer.prebuffer(threading.Event())
        with self.assertRaises(SourceError):
            buffer.frame(1)
        buffer.close()

    def test_replay_limits_are_validated(self):
        ReplaySettings(prebuffer_frames=0, maximum_frames=1).validate()
        ReplaySettings(prebuffer_frames=120, maximum_frames=18_001).validate()
        ReplaySettings(maximum_frames=MAXIMUM_REPLAY_FRAMES).validate()
        for settings in (
            ReplaySettings(prebuffer_frames=-1),
            ReplaySettings(prebuffer_frames=121),
            ReplaySettings(maximum_frames=0),
            ReplaySettings(maximum_frames=MAXIMUM_REPLAY_FRAMES + 1),
            ReplaySettings(clock_start_lead_ms=0),
            ReplaySettings(maximum_clock_offset_ms=0),
        ):
            with self.assertRaises(ValueError):
                settings.validate()

    def test_runner_prefills_before_clock_start_and_honours_frame_limit(self):
        source = FakeSequentialSource(frame_count=5)
        registry = FakeRegistry(source)
        runner = ReplayRunner(
            source,
            FakeEngine(),
            registry,
            settings=ReplaySettings(
                target_fps=0,
                prebuffer_frames=2,
                maximum_frames=3,
            ),
        )
        summary = runner.run()
        self.assertEqual(summary.frames_processed, 3)
        self.assertEqual(summary.prebuffered_frames, 2)
        self.assertEqual(source.calls, [0, 1, 2])
        self.assertEqual(source.released, [0, 1, 2])
        self.assertEqual(
            registry.transitions,
            [
                (RunState.CREATED, 0),
                (RunState.RUNNING, 2),
                (RunState.COMPLETED, 3),
            ],
        )
        performance = registry.sessions[-1].settings["performance"]
        self.assertEqual(performance["prebuffered_frames"], 2)
        self.assertEqual(performance["crops_submitted"], 0)
        self.assertEqual(performance["crops_dropped"], 0)
        self.assertGreater(performance["achieved_fps"], 0)
        self.assertEqual(performance["timings_ms"]["frame_work_ms"]["count"], 3)
        self.assertIn("system", performance)

    def test_replay_clock_starts_after_downstream_workers_are_ready(self):
        source = FakeSequentialSource(frame_count=1)
        registry = FakeRegistry(source)
        dispatcher = SlowStartingDispatcher()

        ReplayRunner(
            source,
            FakeEngine(),
            registry,
            settings=ReplaySettings(
                target_fps=0,
                prebuffer_frames=0,
                maximum_frames=1,
            ),
            crop_dispatcher=dispatcher,
        ).run()

        running = next(
            session
            for session in registry.sessions
            if session.state == RunState.RUNNING
        )
        self.assertGreaterEqual(
            running.clock_monotonic_ns,
            dispatcher.ready_monotonic_ns,
        )

    def test_recorded_run_right_censors_tracks_at_natural_boundary(self):
        source = FakeSequentialSource(frame_count=2)
        registry = FakeRegistry(source)
        engine = BoundaryCensoringEngine()
        dispatcher = SlowStartingDispatcher()

        summary = ReplayRunner(
            source,
            engine,
            registry,
            settings=ReplaySettings(
                target_fps=0,
                prebuffer_frames=0,
                maximum_frames=2,
            ),
            crop_dispatcher=dispatcher,
        ).run()

        self.assertEqual(engine.boundary_timestamps, [10])
        self.assertEqual(summary.right_censored_tracks, 1)
        self.assertEqual(len(dispatcher.frames[-1][0]), 1)
        self.assertIn("run-boundary-cancelled", dispatcher.frames[-1][0][0][2])

    def test_runner_suppresses_and_restores_cyclic_gc(self):
        source = FakeSequentialSource(frame_count=2)
        engine = GcRecordingEngine()
        registry = FakeRegistry(source)
        gc_was_enabled = gc.isenabled()
        if not gc_was_enabled:
            gc.enable()
        try:
            ReplayRunner(
                source,
                engine,
                registry,
                settings=ReplaySettings(
                    target_fps=0,
                    prebuffer_frames=0,
                    maximum_frames=2,
                ),
            ).run()
            self.assertEqual(engine.gc_states, [False, False])
            self.assertTrue(gc.isenabled())
        finally:
            if not gc_was_enabled:
                gc.disable()

    def test_live_runner_uses_capture_clock_without_replay_pacing(self):
        source = FakeLiveSource(frame_count=3)
        registry = FakeRegistry(source)
        started = time.monotonic()
        summary = ReplayRunner(
            source,
            FakeEngine(),
            registry,
            settings=ReplaySettings(
                target_fps=60,
                prebuffer_frames=0,
                maximum_frames=3,
            ),
        ).run()

        self.assertLess(time.monotonic() - started, 0.04)
        self.assertTrue(summary.clock_synchronized)
        self.assertEqual(summary.frames_processed, 3)
        running = next(
            session for session in registry.sessions if session.state == RunState.RUNNING
        )
        self.assertEqual(
            running.settings["clock_contract"]["version"], "fastcap-live-v1"
        )

    def test_registry_startup_stall_cannot_consume_the_run_clock_budget(self):
        source = FakeSequentialSource(frame_count=1)
        registry = DelayedRunningRegistry(source, delay_seconds=0.025)

        summary = ReplayRunner(
            source,
            FakeEngine(),
            registry,
            settings=ReplaySettings(
                target_fps=60,
                prebuffer_frames=0,
                maximum_frames=1,
                clock_start_lead_ms=50,
                maximum_clock_offset_ms=2,
            ),
        ).run()

        running = next(
            session for session in registry.sessions if session.state == RunState.RUNNING
        )
        self.assertTrue(summary.clock_synchronized)
        self.assertEqual(summary.clock_anchor_attempts, 1)
        self.assertEqual(summary.clock_anchor_misses, 0)
        self.assertLess(abs(summary.clock_start_offset_ms), 2.0)
        self.assertGreater(
            running.clock_monotonic_ns - registry.running_returns_ns[0],
            5_000_000,
        )

    def test_runner_drops_stale_frames_and_reports_source_timeline(self):
        source = FakeSequentialSource(frame_count=6)
        registry = FakeRegistry(source)
        summary = ReplayRunner(
            source,
            SlowEngine(),
            registry,
            settings=ReplaySettings(
                target_fps=100,
                prebuffer_frames=0,
                maximum_frames=6,
                maximum_frame_age_ms=1,
            ),
        ).run()

        self.assertGreater(summary.frames_skipped, 0)
        self.assertEqual(summary.frames_processed + summary.frames_skipped, 6)
        self.assertEqual(
            sum(int(item["frame_count"]) for item in summary.stale_skip_events),
            summary.frames_skipped,
        )
        self.assertEqual(
            [int(item["first_frame_index"]) for item in summary.stale_skip_events],
            sorted(
                int(item["first_frame_index"])
                for item in summary.stale_skip_events
            ),
        )
        self.assertGreater(summary.max_frame_age_ms, 0)
        self.assertGreater(summary.source_timeline_fps, summary.achieved_fps)


if __name__ == "__main__":
    unittest.main()
