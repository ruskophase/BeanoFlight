"""Deterministic recorder tests. The fake muxer never accesses a camera."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recorder import VideoRecorder, buffer_policy, recording_jpeg


class Pipe:
    def __init__(self, blocked=True, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.fail = fail
        self.closed = False
        self.payloads = []

    def write(self, data):
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('Test did not release fake output')
        if self.fail:
            raise BrokenPipeError('Synthetic broken output')
        self.payloads.append(data)
        return len(data)

    def close(self):
        self.closed = True


class Process:
    def __init__(self, pipe, code=0):
        self.stdin = pipe
        self.code = code
        self.done = False

    def wait(self, timeout=None):
        self.done = True
        return self.code

    def poll(self):
        return self.code if self.done else None

    def kill(self):
        self.done = True


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(patch.stopall)
        patch('recorder.shutil.which', return_value='/fake/ffmpeg').start()
        # Ignore deployment environment overrides in repeatable unit tests.
        patch.dict(os.environ, {}, clear=True).start()

    def recorder(self, pipe=None, code=0, **kwargs):
        pipe = pipe or Pipe()
        process = Process(pipe, code)
        patch('recorder.subprocess.Popen', return_value=process).start()
        r = VideoRecorder(self.folder.name, 180, **kwargs)
        self.addCleanup(self.finish, r)
        return r

    def finish(self, r):
        r.process.stdin.release.set()
        r.stop()
        r.thread.join(3)
        self.assertFalse(r.running)

    def test_default_policy_and_configuration_validation(self):
        p = buffer_policy(180)
        self.assertEqual(p['frame_limit'], 900)
        self.assertEqual(p['byte_limit'], 64*1024**2)
        with patch.dict(os.environ, {'FASTCAM_RECORDER_BUFFER_SECONDS':'3'}):
            self.assertEqual(buffer_policy(180)['frame_limit'], 540)
        for kw in ({'seconds':0}, {'seconds':float('inf')}, {'byte_limit':0}, {'stall_ms':-1}):
            with self.subTest(kw=kw), self.assertRaises(RuntimeError):
                buffer_policy(180, **kw)

    def test_recovers_beyond_old_180_frame_limit_without_reordering(self):
        r = self.recorder(stall_ms=.000001)
        frames = [b'\xff\xd8'+i.to_bytes(4,'big') for i in range(360)]
        self.assertTrue(r.enqueue(frames[0], 0, 0))
        self.assertTrue(r.process.stdin.entered.wait(1))
        # The output remains blocked while the capture callback queues two seconds.
        for i, frame in enumerate(frames[1:], 1):
            self.assertTrue(r.enqueue(frame, i*5556, i))
        self.assertEqual(r.diagnostics()['peak_buffered_frames'], 360)
        self.assertEqual(r.diagnostics()['inflight_bytes'], len(frames[0]))
        self.finish(r)
        self.assertEqual(r.process.stdin.payloads, frames)
        d = json.loads(r.diagnostics_path.read_text())
        self.assertIsNone(d['error'])
        self.assertTrue(d['finalized'])
        self.assertEqual(d['accepted_frames'], d['written_frames'])
        self.assertEqual(d['written_frames'], 360)
        self.assertEqual(d['buffered_bytes'], 0)
        self.assertGreater(d['stall_count'], 0)
        self.assertTrue(d['queue_samples'])

    def test_frame_limit_includes_inflight_and_failure_time_is_preserved(self):
        r = self.recorder(buffer_seconds=2/180)
        self.assertTrue(r.enqueue(b'1234'))
        self.assertTrue(r.process.stdin.entered.wait(1))
        self.assertTrue(r.enqueue(b'5678'))
        self.assertFalse(r.enqueue(b'next'))
        d = r.diagnostics()
        self.assertEqual((d['buffered_frames'], d['buffered_bytes']), (2,8))
        self.assertEqual(d['failure_reason'], 'frame_limit')
        self.assertIsNotNone(d['failure_at'])
        self.assertFalse(r.enqueue(b'ignored'))
        self.finish(r)
        final = r.diagnostics()
        self.assertEqual(final['failure_at'], d['failure_at'])
        self.assertEqual(final['written_frames'], 2)
        self.assertEqual(final['rejected_frames'], 1)
        self.assertEqual(final['ignored_after_failure'], 1)
        self.assertFalse(final['abandoned_frames'])

    def test_byte_cap_includes_inflight_and_oversize_single_frame_is_rejected(self):
        r = self.recorder(max_buffer_bytes=7)
        self.assertTrue(r.enqueue(b'1234'))
        self.assertTrue(r.process.stdin.entered.wait(1))
        self.assertFalse(r.enqueue(b'5678'))
        self.assertEqual(r.diagnostics()['failure_reason'], 'byte_limit')
        self.assertEqual(r.diagnostics()['peak_buffered_bytes'],4)
        self.finish(r)
        r = self.recorder(max_buffer_bytes=3)
        self.assertFalse(r.enqueue(b'1234'))
        self.finish(r)
        self.assertEqual(r.frames, 0)
        self.assertEqual(r.diagnostics()['peak_buffered_bytes'],0)

    def test_write_failure_accounts_for_abandoned_frames(self):
        r = self.recorder(Pipe(fail=True))
        r.enqueue(b'first')
        self.assertTrue(r.process.stdin.entered.wait(1))
        r.enqueue(b'second')
        self.finish(r)
        d = r.diagnostics()
        self.assertEqual(d['failure_reason'], 'write_failed')
        self.assertEqual(d['abandoned_frames'],2)
        self.assertEqual(d['buffered_bytes'],0)
        self.assertFalse(d['finalized'])

    def test_muxer_error_is_not_reported_as_success(self):
        r = self.recorder(Pipe(blocked=False), code=1)
        r.enqueue(b'image')
        self.finish(r)
        self.assertIn('exited with code 1',r.error)
        self.assertIsNotNone(r.diagnostics()['failure_at'])

    def test_strip_only_empty_camera_app1_without_changing_image(self):
        jpeg = b'\xff\xd8\xff\xe0\x00\x04ok\xff\xda\x00\x02image'
        native = jpeg[:2]+b'\xff\xe1\x00\x04\x00\x00'+jpeg[2:]
        self.assertEqual(recording_jpeg(native),jpeg)
        nonempty = jpeg[:2]+b'\xff\xe1\x00\x04ok'+jpeg[2:]
        self.assertEqual(recording_jpeg(nonempty),nonempty)


if __name__ == '__main__':
    unittest.main()
