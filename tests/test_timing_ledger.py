import unittest
from dataclasses import replace

from test_registry import track

from beanoflight.models import BeanRef
from beanoflight.prediction import GateLayout, TrajectoryPredictor
from beanoflight.registry import BeanRegistry
from beanoflight.registry_models import InferenceJob, InferenceStatus, SortingDecision
from beanoflight.timing_ledger import bean_timing_ledger, summarize_timing_ledgers


class TimingLedgerTests(unittest.TestCase):
    def test_reports_lateness_shadow_recovery_and_line_extension(self):
        bean_ref = BeanRef("timing-run", 1)
        snapshot = track(bean_ref, 2, 100_000_000, 20.0)
        prediction = TrajectoryPredictor(GateLayout(80.0)).predict(snapshot)
        record = BeanRegistry().update_track(snapshot, prediction)
        job = InferenceJob(
            "job-1",
            bean_ref,
            InferenceStatus.COMPLETED,
            "CamL",
            1,
            80_000_000,
            record.revision,
            224,
            224,
            False,
            80_000_000,
            120_000_000,
            source_crop_width_px=160,
            source_crop_height_px=160,
            resized=True,
            timing_marks_ns={
                "first_detection_source_ns": 60_000_000,
                "crop_capture_source_ns": 80_000_000,
                "inference_started_monotonic_ns": 1_000_000_000,
                "inference_completed_monotonic_ns": 1_015_000_000,
                "registry_classification_received_monotonic_ns": 1_016_000_000,
            },
        )
        decision = SortingDecision(
            "decision-1",
            "sorter",
            120_000_000,
            120_000_000,
            (),
            reason="classification arrived too late for safe actuation",
            crossing_timestamp_ns=prediction.crossing_timestamp_ns,
            timing_marks_ns={
                "sorter_event_received_monotonic_ns": 1_018_000_000,
                "available_notice_ns": -3_000_000,
                "minimum_notice_ns": 4_000_000,
                "additional_notice_required_ns": 7_000_000,
                "direct_delivery_acknowledged": 0,
                "direct_delivery_receiver_received_monotonic_ns": 0,
                "direct_delivery_completed_monotonic_ns": 9_000_000_000,
            },
        )
        record = replace(record, inference_jobs=(job,), decision=decision)

        ledger = bean_timing_ledger(record)
        summary = summarize_timing_ledgers((record,))

        self.assertEqual(ledger["result"], "too_late")
        self.assertEqual(ledger["late_by_ms"], 7.0)
        self.assertTrue(ledger["resized_crop"])
        self.assertGreater(ledger["equivalent_line_extension_mm"], 0.0)
        self.assertNotIn(
            "direct_receiver_to_ack_ms", ledger["durations_ms"]
        )
        self.assertEqual(summary["shadow_recovered_with_extra_notice"]["5"], 0)
        self.assertEqual(summary["shadow_recovered_with_extra_notice"]["10"], 1)


if __name__ == "__main__":
    unittest.main()
