import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from beanoflight.analysis import AnalysisEngine
from beanoflight.calibration import MetricPlaneCalibration
from beanoflight.detection import BeanDetector, DetectorSettings
from beanoflight.prediction import GateLayout
from beanoflight.registry import BeanRegistry


class EndToEndAnalysisTests(unittest.TestCase):
    def test_falling_blob_keeps_id_and_gains_gate_prediction(self):
        with tempfile.TemporaryDirectory() as temporary:
            calibration_path = Path(temporary) / "homography.json"
            row_counts = [5, 5, 5, 5]
            calibration_path.write_text(
                json.dumps(
                    {
                        "schema": "pinkplane-homography/v2",
                        "mapping": {"coordinate_domain": "undistorted"},
                        "correspondence": {
                            "row_counts": row_counts,
                            "mean_CamL_points_px": [
                                [100 + column * 50, 70 + row * 50]
                                for row, count in enumerate(row_counts)
                                for column in range(count)
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            calibration = MetricPlaneCalibration.from_pinkplane(
                calibration_path, image_size_px=(400, 300)
            )

        background = np.full((300, 400, 3), 20, dtype=np.uint8)
        detector = BeanDetector(
            DetectorSettings(
                threshold=15,
                min_area_px=300,
                max_area_px=5_000,
                min_width_px=10,
                min_height_px=10,
            )
        )
        layout = GateLayout(calibration.sorting_line_y())
        registry = BeanRegistry()
        engine = AnalysisEngine(
            calibration,
            detector,
            background,
            gate_layout=layout,
            registry=registry,
        )
        results = []
        for index, y in enumerate((30, 103, 184)):
            frame = background.copy()
            cv2.ellipse(frame, (205 + index * 2, y), (24, 17), 20, 0, 360, (190, 170, 140), -1)
            results.append(engine.process(frame, index, index * 16_666_667))

        self.assertEqual([len(result.detections) for result in results], [1, 1, 1])
        ids = [result.tracks[0].bean_ref for result in results]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[1], ids[2])
        self.assertEqual(results[-1].tracks[0].hits, 3)
        self.assertEqual(len(results[-1].predictions), 1)
        self.assertGreater(results[-1].predictions[0].crossing_timestamp_ns, results[-1].timestamp_ns)
        registry_record = registry.get(ids[-1])
        self.assertEqual(registry_record.revision, 3)
        self.assertEqual(registry_record.track.hits, 3)
        self.assertEqual(registry_record.prediction, results[-1].predictions[0])


if __name__ == "__main__":
    unittest.main()
