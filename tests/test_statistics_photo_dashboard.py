import json
import tempfile
import unittest
from pathlib import Path

from beanoflight.source import SourceError
from beanoflight.statistics_photo_dashboard import (
    add_photo_dashboard_to_bundle,
    install_photo_dashboard,
    _source_recording,
)


class PhotoDashboardTests(unittest.TestCase):
    def _bundle(self, root: Path) -> Path:
        recording = root / "recording"
        bundle = recording / "postprocess" / "statistics-bundle"
        (bundle / "photos").mkdir(parents=True)
        (recording / "recording.json").write_text(
            json.dumps(
                {
                    "classification": "test",
                    "plan": {"frame_rate_hz": 60},
                }
            ),
            encoding="utf-8",
        )
        bean = {
            "bean_id": "run/000001",
            "bean_sequence": 1,
            "first_frame_index": 10,
            "sample_count": 3,
            "combined_lab_l_mean": 42.5,
            "projected_area_geomean_mm2_median": 31.2,
            "appearance_outlier_score": 1.2,
            "photo_CamL": "photos/bean-000001-CamL.jpg",
            "photo_CamR": "photos/bean-000001-CamR.jpg",
        }
        (bundle / "beans.jsonl").write_text(json.dumps(bean) + "\n", encoding="utf-8")
        (bundle / "photos/index.json").write_text(
            json.dumps(
                {
                    "schema": "beanoflight-bean-photos/v1",
                    "photos": [
                        {
                            "bean_id": "run/000001",
                            "CamL": bean["photo_CamL"],
                            "CamR": bean["photo_CamR"],
                            "frame_index": 12,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        for camera in ("CamL", "CamR"):
            (bundle / bean[f"photo_{camera}"]).write_bytes(b"jpeg")
        (bundle / "summary.json").write_text("{}\n", encoding="utf-8")
        (bundle / "manifest.json").write_text("{}\n", encoding="utf-8")
        (bundle / "README.md").write_text("# Bundle\n", encoding="utf-8")
        return bundle

    def test_existing_bundle_gets_full_dashboard_with_pairs_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            result = add_photo_dashboard_to_bundle(bundle)
            self.assertEqual(result["paired_beans"], 1)
            encoded = (bundle / "dashboard/batch-data.js").read_text(encoding="utf-8")
            payload = json.loads(
                encoded.removeprefix("window.BEANO_BATCH_DATA=").removesuffix(";\n")
            )
            self.assertEqual(payload["classification"], "test")
            self.assertEqual(payload["source_fps"], 60)
            self.assertEqual(payload["schema"], "beanoflight-statistics-dashboard/v1")
            self.assertEqual(payload["beans"][0][payload["fields"].index("bean_id")], "run/000001")
            self.assertEqual(
                payload["beans"][0][payload["fields"].index("photo_CamL")], "../photos/bean-000001-CamL.jpg"
            )
            self.assertEqual(
                payload["beans"][0][payload["fields"].index("photo_CamR")], "../photos/bean-000001-CamR.jpg"
            )
            self.assertEqual(len(payload["beans"][0]), len(payload["fields"]))
            self.assertTrue((bundle / "dashboard/chart.html").is_file())
            self.assertIn("Measurements and paired photographs", (bundle / "dashboard/index.html").read_text())
            manifest = json.loads(
                (bundle / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "dashboard/index.html", {item["path"] for item in manifest["files"]}
            )
            self.assertEqual(manifest["summary"]["photo_dashboard"]["paired_beans"], 1)

    def test_generated_dashboard_can_be_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            add_photo_dashboard_to_bundle(bundle)
            add_photo_dashboard_to_bundle(bundle)
            self.assertTrue((bundle / "dashboard/chart.html").is_file())

    def test_fresh_bundle_can_install_before_summary_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            (bundle / "summary.json").unlink()
            result = install_photo_dashboard(bundle, summary={"counts": {}})
            self.assertEqual(result["paired_beans"], 1)
            self.assertTrue((bundle / "dashboard/index.html").is_file())

    def test_nested_experimental_bundle_retains_source_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            nested = bundle.parent / "segmentation-alternatives" / "close3"
            nested.mkdir(parents=True)
            self.assertEqual(_source_recording(nested), bundle.parents[1])

    def test_mismatched_photo_link_is_rejected_without_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            bean = json.loads((bundle / "beans.jsonl").read_text(encoding="utf-8"))
            bean["photo_CamR"] = "photos/wrong-CamR.jpg"
            (bundle / "beans.jsonl").write_text(
                json.dumps(bean) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(SourceError, "photo link disagrees"):
                add_photo_dashboard_to_bundle(bundle)
            self.assertFalse((bundle / "dashboard").exists())


if __name__ == "__main__":
    unittest.main()
