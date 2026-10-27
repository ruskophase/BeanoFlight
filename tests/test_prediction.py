import unittest
from dataclasses import replace

from beanoflight.models import BeanRef, TrackSnapshot, TrackStatus
from beanoflight.prediction import GateLayout, TrajectoryPredictor


class PredictionTests(unittest.TestCase):
    def test_predicts_crossing_time_and_central_gate_probability(self):
        track = TrackSnapshot(
            BeanRef("run", 1),
            TrackStatus.CONFIRMED,
            1_000_000_000,
            (0.0, 0.0, 0.0, 900.0),
            tuple(tuple(value for value in row) for row in (
                (0.04, 0.0, 0.0, 0.0),
                (0.0, 0.04, 0.0, 0.0),
                (0.0, 0.0, 25.0, 0.0),
                (0.0, 0.0, 0.0, 25.0),
            )),
            3,
            0,
            (10, 10, 20, 20),
            (),
        )
        prediction = TrajectoryPredictor(GateLayout(67.0)).predict(track)
        self.assertIsNotNone(prediction)
        self.assertGreater(prediction.seconds_until_crossing, 0.0)
        self.assertLess(prediction.seconds_until_crossing, 0.1)
        best = max(prediction.gates, key=lambda item: item.probability)
        self.assertEqual(best.gate.index, 0)
        self.assertGreater(best.probability, 0.4)
        self.assertGreater(prediction.crossing_timestamp_ns, track.timestamp_ns)
        exited_prediction = TrajectoryPredictor(GateLayout(67.0)).predict(
            replace(track, status=TrackStatus.EXITED)
        )
        self.assertIsNotNone(exited_prediction)
        self.assertEqual(exited_prediction.bean_ref, track.bean_ref)
        self.assertIsNone(
            TrajectoryPredictor(GateLayout(67.0)).predict(
                replace(track, status=TrackStatus.TENTATIVE, hits=1)
            )
        )

    def test_gate_layout_requires_central_odd_gate(self):
        with self.assertRaises(ValueError):
            GateLayout(67.0, gate_count=20)


if __name__ == "__main__":
    unittest.main()
