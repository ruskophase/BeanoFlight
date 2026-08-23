import gc
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from beanoflight.crop import CropPayload
from beanoflight.models import BeanRef, FrameAnalysis
from beanoflight.registry_models import InferenceJob, InferenceStatus, RunState
from beanoflight.replay import (
    CropDispatcher,
    DecodedFrameBuffer,
    ReplayRunner,
    ReplaySettings,
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

    def start(self):
        time.sleep(0.01)
        self.ready_monotonic_ns = time.monotonic_ns()

    def enqueue_frame(self, _updates, _payloads):
        return True

    def close(self, *, drain=True):
        return None

    def performance_metrics(self):
        return {}


class ReplayBufferTests(unittest.TestCase):
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
        ReplaySettings(prebuffer_frames=120, maximum_frames=1000).validate()
        for settings in (
            ReplaySettings(prebuffer_frames=-1),
            ReplaySettings(prebuffer_frames=121),
            ReplaySettings(maximum_frames=0),
            ReplaySettings(maximum_frames=1001),
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
        self.assertGreater(summary.max_frame_age_ms, 0)
        self.assertGreater(summary.source_timeline_fps, summary.achieved_fps)


if __name__ == "__main__":
    unittest.main()
