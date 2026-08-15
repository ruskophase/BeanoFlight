import unittest

import numpy as np

from beanoflight.models import Detection, Observation, TrackStatus
from beanoflight.tracking import TrackManager, TrackerSettings, _optimal_assignment


def observation(frame: int, timestamp_ns: int, x: float, y: float, area: int = 1000):
    return Observation(
        frame,
        timestamp_ns,
        Detection((100.0 + x, 100.0 + y), (90, 90, 30, 30), area, 0.9, (50, 80, 120)),
        (x, y),
    )


class TrackingTests(unittest.TestCase):
    def test_id_is_immediate_and_confirms_on_second_observation(self):
        manager = TrackManager(top_y_mm=-37.0, bottom_y_mm=37.0, run_id="abc12345")
        first = manager.update((observation(0, 0, 1.0, -31.0),), 0)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].status, TrackStatus.TENTATIVE)
        self.assertEqual(first[0].bean_ref.sequence, 1)

        second_time = 16_666_667
        second = manager.update(
            (observation(1, second_time, 1.3, -18.0),), second_time
        )
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].bean_ref, first[0].bean_ref)
        self.assertEqual(second[0].status, TrackStatus.CONFIRMED)
        self.assertEqual(second[0].hits, 2)

    def test_midfield_detection_does_not_create_new_id(self):
        manager = TrackManager(top_y_mm=-37.0, bottom_y_mm=37.0, run_id="run")
        snapshots = manager.update((observation(0, 0, 0.0, 5.0),), 0)
        self.assertEqual(snapshots, ())

    def test_small_set_assignment_is_globally_optimal(self):
        costs = np.asarray(((0.1, 0.2), (0.11, 0.9)), dtype=np.float64)
        assignment = set(_optimal_assignment(costs))
        self.assertEqual(assignment, {(0, 1), (1, 0)})

    def test_track_exits_after_miss_below_frame(self):
        settings = TrackerSettings(confirmation_hits=1, maximum_missed_frames=0)
        manager = TrackManager(
            top_y_mm=-37.0, bottom_y_mm=37.0, settings=settings, run_id="run"
        )
        manager.update((observation(0, 0, 0.0, -20.0),), 0)
        ended = manager.update((), 200_000_000)
        self.assertEqual(ended[0].status, TrackStatus.EXITED)
        self.assertEqual(manager.active_count, 0)


if __name__ == "__main__":
    unittest.main()
