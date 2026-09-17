"""Tests for candidate additional-track reconciliation."""

import unittest

from beanoflight.segmentation_review_export import Track, match_tracks


def track(bean_id: str, x: float, first_frame: int = 10) -> Track:
    return Track(
        bean_id=bean_id,
        sequence=int(bean_id),
        first_frame=first_frame,
        last_frame=first_frame + 2,
        photo_frame=first_frame + 1,
        sample_count=3,
        x=x,
        y=100,
        observed_frame=first_frame,
        velocity_x=0,
        velocity_y=100,
        photo_caml="left.jpg",
        photo_camr="right.jpg",
    )


class MatchTracksTest(unittest.TestCase):
    def test_split_leaves_one_candidate_unmatched(self) -> None:
        matched, candidates = match_tracks(
            [track("1", 100)], [track("2", 100), track("3", 120)]
        )
        self.assertEqual(matched, {"2": "1"})
        self.assertEqual(candidates["3"], [("1", 20.0)])

    def test_distant_track_is_additional_candidate(self) -> None:
        matched, candidates = match_tracks(
            [track("1", 100)], [track("2", 100), track("3", 400)]
        )
        self.assertEqual(matched, {"2": "1"})
        self.assertNotIn("3", candidates)


if __name__ == "__main__":
    unittest.main()
