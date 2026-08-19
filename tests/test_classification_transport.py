import tempfile
import threading
import unittest
from pathlib import Path

from beanoflight.classification import CLASSIFICATION_EVIDENCE
from beanoflight.classification_transport import (
    DirectEvidenceTransportError,
    DirectInferenceEvidence,
    ZeroMQDirectEvidencePublisher,
    ZeroMQDirectEvidenceReceiver,
)
from beanoflight.models import BeanRef
from beanoflight.registry_models import (
    Enrichment,
    InferenceJob,
    InferenceStatus,
)


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

            sent, sent_ns = publisher.send_batch("batch-1", (direct_item(),))
            self.assertTrue(sent)
            receive_thread.join(1.0)
            self.assertFalse(receive_thread.is_alive())
            received = received_batches[0]

            self.assertEqual(received.batch_id, "batch-1")
            self.assertEqual(received.sent_monotonic_ns, sent_ns)
            self.assertEqual(received.items, (direct_item(),))
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

            sent, _sent_ns = publisher.send_batch("batch-rejected", (direct_item(),))

            receive_thread.join(1.0)
            self.assertFalse(receive_thread.is_alive())
            self.assertFalse(sent)
            publisher.close()
            receiver.close()


if __name__ == "__main__":
    unittest.main()
