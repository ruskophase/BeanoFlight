import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from beanoflight.statistics_full_frames import write_frame_pack


class FullFramePackTests(unittest.TestCase):
    def test_sampled_frame_pairs_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photos").mkdir()
            (root / "photos/index.json").write_text(json.dumps({"photos": [
                {"samples": [{"frame_index": 12, "right_frame_index": 13}]},
                {"samples": [{"frame_index": 12, "right_frame_index": 13}]},
            ]}), encoding="utf-8")
            with patch("beanoflight.statistics_full_frames.paired_frame_jpegs", return_value=(b"left", b"right", 13)) as encode:
                result = write_frame_pack(root, object())
            self.assertEqual(result["frame_pairs"], 1)
            encode.assert_called_once()
            self.assertEqual((root / "frames/CamL-000012.jpg").read_bytes(), b"left")
            self.assertEqual((root / "frames/CamR-000013.jpg").read_bytes(), b"right")
            index = json.loads((root / "frames/index.json").read_text())
            self.assertEqual(index["frame_pairs"][0]["CamL"], "frames/CamL-000012.jpg")

    def test_conflicting_pair_is_rejected_before_creating_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photos").mkdir()
            (root / "photos/index.json").write_text(json.dumps({"photos": [
                {"frame_index": 12, "right_frame_index": 13},
                {"frame_index": 12, "right_frame_index": 14},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting CamR"):
                write_frame_pack(root, object())
            self.assertFalse((root / "frames").exists())


if __name__ == "__main__":
    unittest.main()
