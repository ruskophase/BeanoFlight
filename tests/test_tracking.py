import unittest

import numpy as np

from beanoflight.models import Detection, Observation, TrackStatus
from beanoflight.tracking import TrackerSettings, TrackManager, _optimal_assignment


def observation(
    frame: int,
    timestamp_ns: int,
    x: float,
    y: float,
    area: int = 1000,
    bbox=(90, 90, 30, 30),
):
    return Observation(
        frame,
        timestamp_ns,
        Detection((100.0 + x, 100.0 + y), bbox, area, 0.9, (50, 80, 120)),
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

    def test_top_clipped_detection_waits_for_complete_crop_before_id_birth(self):
        settings = TrackerSettings(
            left_birth_margin_px=50,
            right_birth_margin_px=50,
        )
        manager = TrackManager(
            top_y_mm=-37.0,
            bottom_y_mm=37.0,
            image_width_px=1456,
            settings=settings,
            run_id="top-entry",
        )

        clipped = Observation(
            420,
            0,
            Detection(
                (980.47, 19.96),
                (924, 0, 122, 56),
                4440,
                0.96,
                (50, 80, 120),
            ),
            (16.48, -35.50),
        )
        nearly_clear = Observation(
            421,
            16_666_000,
            Detection(
                (981.49, 108.60),
                (910, 26, 142, 176),
                18340,
                0.97,
                (50, 80, 120),
            ),
            (16.65, -29.54),
        )
        first_complete = Observation(
            422,
            33_332_000,
            Detection(
                (980.96, 304.76),
                (912, 250, 142, 120),
                11992,
                0.97,
                (50, 80, 120),
            ),
            (16.84, -16.32),
        )
        next_complete = Observation(
            423,
            49_998_000,
            Detection(
                (977.06, 544.60),
                (914, 490, 138, 108),
                10752,
                0.97,
                (50, 80, 120),
            ),
            (16.86, -0.07),
        )

        self.assertEqual(manager.update((clipped,), clipped.timestamp_ns), ())
        self.assertEqual(manager.active_count, 0)
        self.assertEqual(manager.pending_birth_count, 1)
        self.assertTrue(
            manager.last_rejections[0].reason.startswith("top entry pending")
        )

        born = manager.update((nearly_clear,), nearly_clear.timestamp_ns)
        self.assertEqual(len(born), 1)
        self.assertEqual(born[0].bean_ref.sequence, 1)
        self.assertEqual(born[0].history, (nearly_clear,))
        self.assertEqual(born[0].status, TrackStatus.TENTATIVE)
        self.assertEqual(manager.pending_birth_count, 0)

        confirmed = manager.update((first_complete,), first_complete.timestamp_ns)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].bean_ref, born[0].bean_ref)
        self.assertEqual(confirmed[0].status, TrackStatus.CONFIRMED)
        self.assertEqual(confirmed[0].history, (nearly_clear, first_complete))

        third = manager.update((next_complete,), next_complete.timestamp_ns)
        self.assertEqual(len(third), 1)
        self.assertEqual(third[0].bean_ref, born[0].bean_ref)
        self.assertEqual(third[0].hits, 3)

    def test_small_set_assignment_is_globally_optimal(self):
        costs = np.asarray(((0.1, 0.2), (0.11, 0.9)), dtype=np.float64)
        assignment = set(_optimal_assignment(costs))
        self.assertEqual(assignment, {(0, 1), (1, 0)})

    def test_assignment_allows_extra_internal_suppression_tracks(self):
        costs = np.full((24, 2), np.inf, dtype=np.float64)
        costs[0, 0] = 0.1
        costs[23, 1] = 0.2
        self.assertEqual(set(_optimal_assignment(costs)), {(0, 0), (23, 1)})

    def test_track_exits_after_miss_below_frame(self):
        settings = TrackerSettings(confirmation_hits=1, maximum_missed_frames=0)
        manager = TrackManager(
            top_y_mm=-37.0, bottom_y_mm=37.0, settings=settings, run_id="run"
        )
        manager.update((observation(0, 0, 0.0, -20.0),), 0)
        ended = manager.update((), 200_000_000)
        self.assertEqual(ended[0].status, TrackStatus.EXITED)
        self.assertEqual(manager.active_count, 0)

    def test_bounded_live_boundary_right_censors_active_tracks(self):
        manager = TrackManager(
            top_y_mm=-37.0,
            bottom_y_mm=37.0,
            settings=TrackerSettings(confirmation_hits=1),
            run_id="bounded-live",
        )
        active = manager.update((observation(0, 10, 0.0, -20.0),), 10)

        censored = manager.cancel_active_at_boundary(20)

        self.assertEqual(censored[0].bean_ref, active[0].bean_ref)
        self.assertEqual(censored[0].status, TrackStatus.CANCELLED)
        self.assertEqual(censored[0].timestamp_ns, 20)
        self.assertEqual(manager.active_count, 0)

    def test_new_detection_touching_left_margin_is_explicitly_rejected(self):
        settings = TrackerSettings(
            confirmation_hits=1,
            left_birth_margin_px=50,
            right_birth_margin_px=40,
        )
        manager = TrackManager(
            top_y_mm=-37.0,
            bottom_y_mm=37.0,
            image_width_px=200,
            settings=settings,
            run_id="run",
        )
        snapshots = manager.update(
            (observation(0, 0, 0.0, -30.0, bbox=(30, 20, 25, 25)),), 0
        )
        self.assertEqual(snapshots, ())
        self.assertEqual(manager.active_count, 0)
        self.assertEqual(manager.suppressed_count, 1)
        self.assertEqual(len(manager.last_rejections), 1)
        self.assertEqual(manager.last_rejections[0].reason, "left birth margin")

        continuation = manager.update(
            (
                observation(
                    1,
                    16_666_667,
                    0.0,
                    -18.0,
                    bbox=(70, 90, 25, 25),
                ),
            ),
            16_666_667,
        )
        self.assertEqual(continuation, ())
        self.assertEqual(manager.active_count, 0)
        self.assertEqual(manager.suppressed_count, 1)
        self.assertEqual(
            manager.last_rejections[0].reason,
            "continuation of left birth margin",
        )

    def test_existing_track_keeps_id_when_later_measurement_enters_margin(self):
        settings = TrackerSettings(
            confirmation_hits=1,
            left_birth_margin_px=50,
            right_birth_margin_px=40,
        )
        manager = TrackManager(
            top_y_mm=-37.0,
            bottom_y_mm=37.0,
            image_width_px=200,
            settings=settings,
            run_id="run",
        )
        first = manager.update(
            (observation(0, 0, 0.0, -30.0, bbox=(60, 20, 25, 25)),), 0
        )
        later = manager.update(
            (
                observation(
                    1,
                    16_666_667,
                    0.0,
                    -18.0,
                    bbox=(20, 90, 25, 25),
                ),
            ),
            16_666_667,
        )
        self.assertEqual(later[0].bean_ref, first[0].bean_ref)
        self.assertEqual(manager.last_rejections, ())

    def test_new_detection_touching_right_margin_is_explicitly_rejected(self):
        settings = TrackerSettings(
            confirmation_hits=1,
            left_birth_margin_px=20,
            right_birth_margin_px=40,
        )
        manager = TrackManager(
            top_y_mm=-37.0,
            bottom_y_mm=37.0,
            image_width_px=200,
            settings=settings,
            run_id="run",
        )
        snapshots = manager.update(
            (observation(0, 0, 0.0, -30.0, bbox=(150, 20, 25, 25)),), 0
        )
        self.assertEqual(snapshots, ())
        self.assertEqual(manager.last_rejections[0].reason, "right birth margin")


if __name__ == "__main__":
    unittest.main()
