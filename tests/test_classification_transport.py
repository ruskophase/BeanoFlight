import tempfile
import threading
import time
import unittest
from pathlib import Path

from beanoflight.classification import CLASSIFICATION_EVIDENCE
from beanoflight.classification_transport import (
    DirectEvidenceBatch,
    DirectEvidenceTransportError,
    DirectInferenceEvidence,
    ZeroMQDirectEvidencePublisher,
    ZeroMQDirectEvidenceReceiver,
)
from beanoflight.models import BeanRef, TrackSnapshot, TrackStatus
from beanoflight.registry_models import (
    Enrichment,
    InferenceJob,
    InferenceStatus,
)
from beanoflight.sorter import SorterService
from beanoflight.sorting_context_transport import SortingContext


def direct_item() -> DirectInferenceEvidence:
    bean_ref = BeanRef("direct-run", 1)
    job = InferenceJob(
        "job-1",
        bean_ref,
        InferenceStatus.SUBMITTED,
        "CamL",
        10,
        100,
        1,
        224,
        224,
        False,
        100,
        100,
    )
    enrichment = Enrichment(
        "mock",
        CLASSIFICATION_EVIDENCE,
        {
            "category": "mould",
            "class_order": ["acceptable", "mould"],
            "probabilities": [0.1, 0.9],
            "ensemble": {
                "id": "direct-run:1:model",
                "sample_index": 1,
                "expected_samples": 2,
            },
        },
        120,
        result_id=job.job_id,
        confidence=0.9,
    )
    return DirectInferenceEvidence(job, enrichment)


class DirectClassificationTransportTests(unittest.TestCase):
    def test_ipc_transport_owns_dedicated_io_contexts(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'dedicated.sock'}"
            receiver = ZeroMQDirectEvidenceReceiver(endpoint)
            publisher = ZeroMQDirectEvidencePublisher(endpoint)

            self.assertTrue(receiver._owns_context)
            self.assertTrue(publisher._owns_context)
            self.assertIsNot(receiver.context, publisher.context)

            publisher.close()
            receiver.close()

    def test_probability_evidence_batch_round_trips_without_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'evidence.sock'}"
            receiver = ZeroMQDirectEvidenceReceiver(endpoint)
            publisher = ZeroMQDirectEvidencePublisher(endpoint)
            received_batches = []
            receive_thread = threading.Thread(
                target=lambda: received_batches.append(receiver.receive_batch()),
                daemon=True,
            )
            receive_thread.start()

            receipt = publisher.send_batch("batch-1", (direct_item(),))
            receive_thread.join(1.0)
            self.assertFalse(receive_thread.is_alive())
            received = received_batches[0]
            delivery = receipt.wait(1.0)

            self.assertEqual(received.batch_id, "batch-1")
            self.assertTrue(delivery.terminal)
            self.assertTrue(delivery.acknowledged)
            self.assertEqual(delivery.attempts, 1)
            self.assertEqual(
                received.sent_monotonic_ns,
                delivery.first_sent_monotonic_ns,
            )
            self.assertEqual(received.items, (direct_item(),))
            publisher.close()
            receiver.close()

    def test_embedded_sorting_context_round_trips_with_evidence(self):
        item = direct_item()
        snapshot = TrackSnapshot(
            item.job.bean_ref,
            TrackStatus.CONFIRMED,
            item.job.capture_timestamp_ns,
            (0.0, -20.0, 1.0, 800.0),
            tuple(
                tuple(0.0 for _ in range(4)) for _ in range(4)
            ),
            3,
            0,
            (20, 20, 60, 60),
            (),
        )
        contextual = DirectInferenceEvidence(
            item.job,
            item.enrichment,
            SortingContext(snapshot, None),
        )
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'context-evidence.sock'}"
            receiver = ZeroMQDirectEvidenceReceiver(endpoint)
            publisher = ZeroMQDirectEvidencePublisher(endpoint)
            received = []
            thread = threading.Thread(
                target=lambda: received.append(receiver.receive_batch()),
                daemon=True,
            )
            thread.start()

            receipt = publisher.send_batch("context-batch", (contextual,))
            thread.join(1.0)

            self.assertTrue(receipt.wait(1.0).acknowledged)
            self.assertEqual(received[0].items, (contextual,))
            publisher.close()
            receiver.close()

    def test_non_evidence_enrichment_is_rejected(self):
        item = direct_item()
        invalid = DirectInferenceEvidence(
            item.job,
            Enrichment(
                "mock",
                "classification_pooled",
                item.enrichment.value,
                120,
                result_id=item.job.job_id,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'evidence.sock'}"
            publisher = ZeroMQDirectEvidencePublisher(endpoint)
            with self.assertRaises(DirectEvidenceTransportError):
                publisher.send_batch("batch-1", (invalid,))
            publisher.close()

    def test_admission_rejection_is_negatively_acknowledged(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'evidence.sock'}"
            receiver = ZeroMQDirectEvidenceReceiver(endpoint)
            publisher = ZeroMQDirectEvidencePublisher(
                endpoint,
                acknowledgement_timeout_ms=100,
                maximum_attempts=1,
            )
            receive_thread = threading.Thread(
                target=lambda: receiver.receive_batch(
                    accept=lambda _batch, _received_ns: False
                ),
                daemon=True,
            )
            receive_thread.start()

            receipt = publisher.send_batch("batch-rejected", (direct_item(),))

            receive_thread.join(1.0)
            self.assertFalse(receive_thread.is_alive())
            delivery = receipt.wait(1.0)
            self.assertTrue(delivery.terminal)
            self.assertFalse(delivery.acknowledged)
            self.assertEqual(delivery.attempts, 1)
            publisher.close()
            receiver.close()

    def test_publisher_retries_a_negatively_acknowledged_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'evidence.sock'}"
            receiver = ZeroMQDirectEvidenceReceiver(endpoint)
            publisher = ZeroMQDirectEvidencePublisher(
                endpoint,
                acknowledgement_timeout_ms=20,
                maximum_attempts=2,
            )
            received = []

            def receive_twice():
                received.append(
                    receiver.receive_batch(
                        accept=lambda _batch, _received_ns: False
                    )
                )
                received.append(receiver.receive_batch())

            receive_thread = threading.Thread(target=receive_twice, daemon=True)
            receive_thread.start()
            receipt = publisher.send_batch("batch-retry", (direct_item(),))

            receive_thread.join(1.0)
            self.assertFalse(receive_thread.is_alive())
            delivery = receipt.wait(1.0)
            self.assertTrue(delivery.acknowledged)
            self.assertEqual(delivery.attempts, 2)
            self.assertIsNone(received[0])
            self.assertEqual(received[1].batch_id, "batch-retry")
            publisher.close()
            receiver.close()

    def test_slow_acknowledgement_does_not_block_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = f"ipc://{Path(temporary) / 'evidence.sock'}"
            receiver = ZeroMQDirectEvidenceReceiver(endpoint)
            publisher = ZeroMQDirectEvidencePublisher(
                endpoint,
                acknowledgement_timeout_ms=100,
            )

            def receive_slowly():
                receiver.receive_batch(
                    accept=lambda _batch, _received_ns: (
                        time.sleep(0.025) is None
                    )
                )
                receiver.receive_batch()

            receive_thread = threading.Thread(target=receive_slowly, daemon=True)
            receive_thread.start()
            started = time.monotonic()
            first = publisher.send_batch("batch-slow", (direct_item(),))
            second = publisher.send_batch("batch-following", (direct_item(),))
            submit_ms = (time.monotonic() - started) * 1_000

            self.assertLess(submit_ms, 5.0)
            self.assertTrue(first.wait(1.0).acknowledged)
            self.assertTrue(second.wait(1.0).acknowledged)
            receive_thread.join(1.0)
            self.assertFalse(receive_thread.is_alive())
            publisher.close()
            receiver.close()

    def test_sorter_acknowledges_duplicate_batch_without_requeueing(self):
        service = SorterService(
            classification_endpoint="",
            sorting_context_endpoint="",
        )
        item = direct_item()
        batch = DirectEvidenceBatch("batch-duplicate", 100, (item,))

        self.assertTrue(service._admit_direct_evidence(batch, 110))
        self.assertTrue(service._admit_direct_evidence(batch, 120))
        self.assertEqual(service._direct_ingress.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
