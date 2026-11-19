import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from beanoflight.statistics_frame_viewer import matching_recording


class FrameViewerTests(unittest.TestCase):
    def test_recording_hash_prevents_wrong_full_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "recording"
            bundle = recording / "postprocess/statistics-bundle"
            bundle.mkdir(parents=True)
            encoded = b'{"schema":"test"}'
            (recording / "recording.json").write_bytes(encoded)
            digest = hashlib.sha256(encoded).hexdigest()
            manifest = {"provenance": {"recording_manifest": {"sha256": digest}}}
            (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(matching_recording(bundle), recording)
            (recording / "recording.json").write_bytes(b'{"schema":"different"}')
            self.assertIsNone(matching_recording(bundle))


if __name__ == "__main__":
    unittest.main()
