"""Optional integration tests of the patched standalone controller with fakes.

FASTCAMUSB_TEST_APP points at staged sources, never the running service.
"""
import importlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.getenv('FASTCAMUSB_TEST_APP'), 'Set FASTCAMUSB_TEST_APP to staged Pi sources')
class ControllerTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.environ['FASTCAMUSB_TEST_APP'])
        self.addCleanup(sys.path.pop,0)
        self.module = importlib.import_module('controller')
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.c = self.module.CameraController(Path(folder.name)/'settings.json')

    def old_failure(self):
        old_stream=SimpleNamespace(running=False,ready=False,error=None,control_error=None,
            last_frame_time=None,exposure_ms=3,camera_fps=200,effective_fps=180,rates=lambda:(0,0,0,0,0,0))
        recorder=SimpleNamespace(running=False,error='Old failure',frames=2646,message=None,
            path=Path('/old.avi'),timing_path=Path('/old.csv'),
            diagnostics=lambda:{'error':'Old failure','failure_at':'original-time'})
        self.c.stream=self.c.recorder_stream=old_stream
        self.c.recorder=recorder
        return old_stream,recorder

    def test_new_stream_does_not_inherit_old_error_or_recording_counts(self):
        old_stream,old_recorder=self.old_failure()
        self.assertEqual(self.c.status()['error'],'Old failure')
        class FakeStream:
            def __init__(stream,args,on_frame):
                stream.__dict__.update(old_stream.__dict__)
                stream.start=lambda:setattr(stream,'running',True)
        with patch.object(self.module,'CameraStream',FakeStream):
            self.c._start()
        d=self.c.status()
        self.assertIsNone(d['error'])
        self.assertEqual(d['recorded_frames'],0)
        self.assertIsNone(d['video_path'])
        self.assertEqual(d['previous_recording']['failure_at'],'original-time')
        self.assertEqual(d['recorder_policy']['frame_limit'],900)

    def test_start_refuses_to_replace_stream_while_recorder_drains(self):
        _,r=self.old_failure()
        r.running=True
        with self.assertRaisesRegex(self.module.ControlError,'still finishing'):
            self.c._start()

    def test_callback_only_enqueues_into_owning_stream(self):
        old,r=self.old_failure();calls=[]
        r.enqueue=lambda *args:calls.append(args)
        self.c._frame(b'one',1,1)
        self.c.stream=object()
        self.c._frame(b'two',2,2)
        self.assertEqual(calls,[(b'one',1,1)])


if __name__=='__main__':
    unittest.main()
