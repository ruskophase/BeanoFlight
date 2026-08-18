import threading
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from beanoflight.models import FrameAnalysis
from beanoflight.registry_models import RunState
from beanoflight.replay import DecodedFrameBuffer, ReplayRunner, ReplaySettings
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


class ReplayBufferTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
