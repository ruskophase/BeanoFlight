import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from beanoflight.analysis import AnalysisRun, export_run_json
from beanoflight.background import (
    DEFAULT_BACKGROUND_FRAMES_TEXT,
    BackgroundProvenance,
    parse_background_frame_indices,
    stratified_random_candidates,
)
from beanoflight.detection import temporal_median_background


class BackgroundSelectionTests(unittest.TestCase):
    def test_manual_background_default_and_recording_bounds(self):
        self.assertEqual(DEFAULT_BACKGROUND_FRAMES_TEXT, "43,222,347")
        self.assertEqual(
            parse_background_frame_indices(
                DEFAULT_BACKGROUND_FRAMES_TEXT,
                frame_count=601,
            ),
            (43, 222, 347),
        )
        for invalid in ("43,222", "43,43,347", "43,-1,347", "43,222,601"):
            with self.assertRaises(ValueError):
                parse_background_frame_indices(invalid, frame_count=601)

    def test_first_candidate_pass_covers_even_temporal_strata(self):
        indices = stratified_random_candidates(1_100, 11, seed=12345)
        self.assertEqual(len(indices), 44)
        for stratum, index in enumerate(indices[:11]):
            self.assertGreaterEqual(index, stratum * 100)
            self.assertLess(index, (stratum + 1) * 100)
        self.assertEqual(
            indices, stratified_random_candidates(1_100, 11, seed=12345)
        )

    def test_replacement_passes_revisit_each_temporal_stratum(self):
        indices = stratified_random_candidates(
            1_100, 11, candidates_per_stratum=4, seed=12345
        )
        for pass_index in range(4):
            for stratum, index in enumerate(
                indices[pass_index * 11 : (pass_index + 1) * 11]
            ):
                self.assertGreaterEqual(index, stratum * 100)
                self.assertLess(index, (stratum + 1) * 100)

    def test_eleven_confirmed_frames_produce_pixel_median(self):
        frames = [np.full((4, 5, 3), value, np.uint8) for value in range(11)]
        result = temporal_median_background(frames)
        self.assertTrue(np.all(result == 5))

    def test_short_video_returns_each_frame_once(self):
        self.assertEqual(
            set(stratified_random_candidates(4, 11, seed=1)),
            {0, 1, 2, 3},
        )

    def test_background_selection_is_exported_as_provenance(self):
        provenance = BackgroundProvenance(
            "human-confirmed stratified temporal median", (4, 20, 37), 9981
        )
        run = AnalysisRun("run", Path("CamL-calibrated.mkv"), True, (), provenance)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "analysis.json"
            export_run_json(run, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["background"]["frame_indices"], [4, 20, 37])
        self.assertEqual(payload["background"]["candidate_seed"], 9981)


if __name__ == "__main__":
    unittest.main()
