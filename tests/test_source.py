import csv
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from beanoflight.source import RecordingVideoSource


class VideoSourceTests(unittest.TestCase):
    def test_uses_exact_fastcap_pair_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "CamL-calibrated.avi"
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (64, 48), True
            )
            self.assertTrue(writer.isOpened())
            for value in (10, 20, 30):
                writer.write(np.full((48, 64, 3), value, dtype=np.uint8))
            writer.release()
            with (directory / "pairs.csv").open("w", newline="", encoding="utf-8") as stream:
                output = csv.DictWriter(stream, fieldnames=("left_timestamp_ns",))
                output.writeheader()
                for timestamp in (100, 33_333_500, 66_667_200):
                    output.writerow({"left_timestamp_ns": timestamp})

            with RecordingVideoSource(path) as source:
                self.assertTrue(source.metadata.exact_timestamps)
                self.assertEqual(source.timestamp_ns(1), 33_333_500)
                self.assertEqual(source.frame(2).shape, (48, 64, 3))
                self.assertEqual(source.frame(0).shape, (48, 64, 3))


if __name__ == "__main__":
    unittest.main()
