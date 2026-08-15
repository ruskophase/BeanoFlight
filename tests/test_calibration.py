import json
import tempfile
import unittest
from pathlib import Path

from beanoflight.calibration import CalibrationError, MetricPlaneCalibration


def write_pinkplane(path: Path, *, coordinate_domain: str = "undistorted") -> None:
    rows = [5, 5, 5, 5]
    points = [
        [100.0 + column * 50.0, 80.0 + row * 50.0]
        for row, count in enumerate(rows)
        for column in range(count)
    ]
    path.write_text(
        json.dumps(
            {
                "schema": "pinkplane-homography/v2",
                "mapping": {"coordinate_domain": coordinate_domain},
                "correspondence": {
                    "row_counts": rows,
                    "mean_CamL_points_px": points,
                },
            }
        ),
        encoding="utf-8",
    )


class MetricCalibrationTests(unittest.TestCase):
    def test_fits_grid_and_centres_metric_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "homography.json"
            write_pinkplane(path)
            calibration = MetricPlaneCalibration.from_pinkplane(
                path, image_size_px=(400, 300), hole_pitch_mm=9.16
            )

        self.assertLess(calibration.rms_error_mm, 1e-6)
        self.assertLess(calibration.max_error_mm, 1e-6)
        centre = calibration.pixel_to_mm((199.5, 149.5))
        self.assertAlmostEqual(centre[0], 0.0, places=8)
        self.assertAlmostEqual(centre[1], 0.0, places=8)
        first = calibration.pixel_to_mm((100.0, 80.0))
        second = calibration.pixel_to_mm((150.0, 80.0))
        self.assertAlmostEqual(second[0] - first[0], 9.16, places=6)
        round_trip = calibration.mm_to_pixel(calibration.pixel_to_mm((123.4, 211.2)))
        self.assertAlmostEqual(round_trip[0], 123.4, places=6)
        self.assertAlmostEqual(round_trip[1], 211.2, places=6)
        self.assertAlmostEqual(calibration.sorting_line_y() - calibration.bottom_y_mm, 30.0)

    def test_rejects_distorted_correspondence_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "homography.json"
            write_pinkplane(path, coordinate_domain="native_distorted")
            with self.assertRaisesRegex(CalibrationError, "undistorted"):
                MetricPlaneCalibration.from_pinkplane(path)


if __name__ == "__main__":
    unittest.main()
