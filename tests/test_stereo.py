import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from beanoflight.stereo import (
    StereoCalibrationError,
    StereoPairMetadata,
    StereoPointCalibration,
)


def _profile(distortion=None):
    return {
        "calibration": {
            "camera_matrix": [
                [600.0, 0.0, 728.0],
                [0.0, 600.0, 544.0],
                [0.0, 0.0, 1.0],
            ],
            "distortion_coefficients": (
                distortion
                if distortion is not None
                else [-0.08, 0.01, 0.001, -0.002, 0.0]
            ),
        }
    }


class StereoTests(unittest.TestCase):
    def test_pair_metadata_round_trip_preserves_sync_and_centroids(self):
        pair = StereoPairMetadata(
            7,
            8,
            1_000_000,
            1_000_900,
            (400.5, 200.25),
            (1_020.0, 201.0),
            (1_017.5, 204.0),
            3.91,
            2_450,
        )

        restored = StereoPairMetadata.from_json(pair.to_json())

        self.assertEqual(restored, pair)
        self.assertEqual(restored.synchronization_delta_ns, 900)

    def test_identity_homography_round_trips_a_distorted_sensor_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "homography.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "pinkplane-homography/v2",
                        "mapping": {
                            "direction": "CamL pixels to CamR pixels",
                            "coordinate_domain": "undistorted",
                            "matrix": np.eye(3).tolist(),
                        },
                    }
                ),
                encoding="utf-8",
            )
            calibration = StereoPointCalibration.load(
                path,
                _profile(),
                _profile(),
            )

            projected = calibration.project_distorted_caml_to_distorted_camr(
                (232.75, 814.5)
            )

        self.assertAlmostEqual(projected[0], 232.75, delta=0.002)
        self.assertAlmostEqual(projected[1], 814.5, delta=0.002)

    def test_rejects_homography_in_a_distorted_coordinate_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "homography.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "pinkplane-homography/v2",
                        "mapping": {
                            "direction": "CamL pixels to CamR pixels",
                            "coordinate_domain": "distorted",
                            "matrix": np.eye(3).tolist(),
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                StereoCalibrationError, "must be undistorted"
            ):
                StereoPointCalibration.load(path, _profile(), _profile())


if __name__ == "__main__":
    unittest.main()
