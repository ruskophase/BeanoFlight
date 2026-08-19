import threading
import unittest
from dataclasses import replace
from typing import ClassVar
from unittest.mock import patch

import numpy as np

from beanoflight.crop import CropPayload
from beanoflight.mock_inference import (
    MockInferencerService,
    MockInferenceSettings,
    _batch_priority_deadline_ns,
    _complete_inference_batch,
    _registry_capabilities,
    _stable_job_key,
)
from beanoflight.models import BeanRef
from beanoflight.registry_models import InferenceJob, InferenceStatus


class _Session:
    def monotonic_to_source_ns(self, monotonic_ns: int) -> int:
        return monotonic_ns


class _RegistryClient:
    instances: ClassVar[list["_RegistryClient"]] = []

    def __init__(self, *_args, **_kwargs):
        self.completions = []
        self.failures = []
        self.__class__.instances.append(self)

    def get_session(self, _run_id):
        return _Session()

    def complete_inference_job(self, bean_ref, job_id, enrichment, **_kwargs):
        self.completions.append((bean_ref, job_id, enrichment))

    def update_inference_job(self, bean_ref, job_id, status, *_args, **_kwargs):
        self.failures.append((bean_ref, job_id, status))

    def close(self):
        pass


def _payload(sequence: int) -> CropPayload:
    bean_ref = BeanRef("batch-run", sequence)
    job = InferenceJob(
        job_id=f"job-{sequence}",
        bean_ref=bean_ref,
        status=InferenceStatus.ACCEPTED,
        camera_id="CamL",
        frame_index=20,
        capture_timestamp_ns=100,
        source_registry_revision=2,
        crop_width_px=4,
        crop_height_px=4,
        padded=False,
        submitted_timestamp_ns=100,
        updated_timestamp_ns=100,
    )
    return CropPayload(job, np.zeros((4, 4, 3), dtype=np.uint8))


class MockInferenceSettingsTests(unittest.TestCase):
    def test_classification_seed_key_is_stable_across_run_and_crop_frame(self):
        first = _payload(3).job
        second = replace(
            first,
            bean_ref=BeanRef("another-run", 3),
            frame_index=99,
        )

        self.assertEqual(_stable_job_key(first), _stable_job_key(second))

    def test_default_curve_models_stereo_batch_latency(self):
        settings = MockInferenceSettings()

        self.assertEqual(settings.nominal_batch_latency_ms(1), 15.0)
        self.assertEqual(settings.nominal_batch_latency_ms(2), 18.0)
        self.assertEqual(settings.nominal_batch_latency_ms(3), 20.5)
        self.assertEqual(settings.nominal_batch_latency_ms(4), 23.0)
        self.assertEqual(settings.nominal_batch_latency_ms(8), 32.0)
        self.assertEqual(settings.nominal_batch_latency_ms(10), 38.0)

    def test_rejects_multiple_workers_for_one_simulated_gpu(self):
        with self.assertRaisesRegex(ValueError, "one GPU"):
            MockInferenceSettings(worker_count=2).validate()


class MockInferenceBatchTests(unittest.TestCase):
    def setUp(self):
        _RegistryClient.instances.clear()

    def test_four_jobs_complete_in_one_logical_stereo_batch(self):
        activities = []
        settings = MockInferenceSettings(
            latency_ms=0,
            jitter_ms=0,
            result_deadline_ms=1_000,
            max_batch_beans=4,
            tail_probability=0,
            categories=("mould",),
            weights=(1.0,),
        )
        service = MockInferencerService(
            settings=settings,
            activity=activities.append,
        )
        self.assertTrue(
            service._accept_batch(tuple(_payload(sequence) for sequence in range(1, 5)))
        )

        with patch(
            "beanoflight.mock_inference.ZeroMQRegistryClient", _RegistryClient
        ):
            worker = threading.Thread(target=service._worker_loop, daemon=True)
            publisher = threading.Thread(target=service._result_loop, daemon=True)
            worker.start()
            publisher.start()
            service._queue.join()
            service._results.join()
            service._stop.set()
            worker.join(1.0)
            publisher.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertFalse(publisher.is_alive())
        stats = service.statistics()
        self.assertEqual(stats["batches"], 1)
        self.assertEqual(stats["completed"], 4)
        self.assertEqual(stats["max_batch_size"], 4)
        self.assertEqual(len(_RegistryClient.instances), 1)
        completions = _RegistryClient.instances[0].completions
        self.assertEqual(len(completions), 4)
        enrichment = completions[0][2]
        self.assertEqual(enrichment.kind, "classification_evidence")
        self.assertEqual(
            enrichment.value["class_order"],
            ["mould"],
        )
        self.assertEqual(enrichment.value["probabilities"], [1.0])
        self.assertEqual(enrichment.value["ensemble"]["sample_index"], 1)
        self.assertEqual(enrichment.value["ensemble"]["expected_samples"], 1)
        inference = enrichment.value["inference"]
        self.assertEqual(inference["input_mode"], "logical_stereo")
        self.assertEqual(inference["transported_camera"], "CamL")
        self.assertEqual(inference["transported_views"], 1)
        self.assertFalse(inference["stereo_pair_complete"])
        self.assertEqual(inference["logical_views"], 2)
        self.assertEqual(inference["batch_beans"], 4)
        self.assertEqual(inference["batch_images"], 8)
        batch_activities = [item for item in activities if item.kind == "batch"]
        self.assertEqual(len(batch_activities), 1)
        self.assertEqual(batch_activities[0].batch_beans, 4)
        self.assertEqual(batch_activities[0].batch_images, 8)
        self.assertEqual(stats["deadline_misses"], 0)

    def test_oversized_source_frame_batch_is_rejected_atomically(self):
        service = MockInferencerService(
            settings=MockInferenceSettings(
                max_batch_beans=2,
                tail_probability=0,
            )
        )

        self.assertFalse(
            service._accept_batch(tuple(_payload(sequence) for sequence in range(1, 4)))
        )
        self.assertEqual(service.statistics()["dropped"], 3)
        self.assertEqual(service._queue.qsize(), 0)

    def test_physical_crossing_estimate_sets_batch_priority(self):
        relaxed = _payload(1)
        urgent = _payload(2)
        relaxed = replace(
            relaxed,
            job=replace(
                relaxed.job,
                capture_timestamp_ns=1_000,
                timing_marks_ns={
                    "inference_priority_crossing_source_ns": 101_001_000
                },
            ),
        )
        urgent = replace(
            urgent,
            job=replace(
                urgent.job,
                capture_timestamp_ns=1_000,
                timing_marks_ns={
                    "inference_priority_crossing_source_ns": 11_001_000
                },
            ),
        )

        relaxed_deadline = _batch_priority_deadline_ns(
            (relaxed,), 1_000_000, 60.0
        )
        urgent_deadline = _batch_priority_deadline_ns(
            (urgent,), 2_000_000, 60.0
        )

        self.assertLess(urgent_deadline, relaxed_deadline)

    def test_legacy_registry_uses_supported_batch_completion(self):
        class LegacyRegistry:
            def __init__(self):
                self.ack_calls = 0
                self.batches = []

            def ping(self):
                return {"service": "BeanRegistry", "schema": 3}

            def complete_inference_jobs_ack(self, _completions):
                self.ack_calls += 1
                raise AssertionError("unsupported acknowledgement must not be called")

            def complete_inference_jobs(self, completions):
                self.batches.append(completions)

        registry = LegacyRegistry()
        completions = (("bean", "job", "result", {}, "event"),)

        capabilities = _registry_capabilities(registry)
        _complete_inference_batch(registry, completions, capabilities)

        self.assertEqual(capabilities, frozenset())
        self.assertEqual(registry.ack_calls, 0)
        self.assertEqual(registry.batches, [completions])


if __name__ == "__main__":
    unittest.main()
