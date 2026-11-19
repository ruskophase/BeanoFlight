import unittest

from beanoflight.classification import (
    CLASSIFICATION_DECISION_BASIS,
    CLASSIFICATION_EVIDENCE,
    classification_decision_basis,
    pool_classification_evidence,
    validate_classification_enrichment,
)
from beanoflight.registry_models import Enrichment


def evidence(
    index: int,
    probabilities: tuple[float, ...],
    *,
    expected: int = 2,
) -> Enrichment:
    classes = ("acceptable", "insect_damage", "mould", "broken")
    winner = max(range(len(classes)), key=probabilities.__getitem__)
    return Enrichment(
        "mock-inferencer",
        CLASSIFICATION_EVIDENCE,
        {
            "category": classes[winner],
            "class_order": list(classes),
            "probabilities": list(probabilities),
            "logits": [0.0, 0.0, 0.0, 0.0],
            "ensemble": {
                "id": "run:1:model",
                "sample_index": index,
                "expected_samples": expected,
            },
        },
        100 + index,
        result_id=f"job-{index}",
        confidence=probabilities[winner],
    )


class ClassificationPoolingTests(unittest.TestCase):
    def test_mean_probability_pool_preserves_runner_up_evidence(self):
        first = evidence(1, (0.51, 0.01, 0.47, 0.01))
        second = evidence(2, (0.01, 0.51, 0.47, 0.01))

        pooled = pool_classification_evidence(
            (first, second), deadline_fallback=False
        )

        self.assertEqual(pooled.value["category"], "mould")
        self.assertAlmostEqual(pooled.confidence, 0.47)
        self.assertEqual(pooled.value["ensemble"]["sample_count"], 2)
        self.assertFalse(pooled.value["ensemble"]["deadline_fallback"])
        validate_classification_enrichment(pooled)

    def test_deadline_fallback_is_an_auditable_single_sample_pool(self):
        first = evidence(1, (0.05, 0.05, 0.85, 0.05))

        pooled = pool_classification_evidence(
            (first,), deadline_fallback=True, timestamp_ns=500
        )

        self.assertEqual(pooled.timestamp_ns, 500)
        self.assertEqual(pooled.value["category"], "mould")
        self.assertEqual(pooled.value["ensemble"]["sample_count"], 1)
        self.assertTrue(pooled.value["ensemble"]["deadline_fallback"])

    def test_deadline_fallback_pools_two_of_three_available_samples(self):
        first = evidence(1, (0.8, 0.2, 0.0, 0.0), expected=3)
        second = evidence(2, (0.4, 0.6, 0.0, 0.0), expected=3)

        pooled = pool_classification_evidence(
            (first, second), deadline_fallback=True, timestamp_ns=500
        )

        ensemble = pooled.value["ensemble"]
        self.assertEqual(ensemble["sample_count"], 2)
        self.assertEqual(ensemble["expected_samples"], 3)
        self.assertEqual(
            ensemble["pooling_method"], "deadline-mean-probability-v1"
        )
        for actual, expected in zip(
            pooled.value["probabilities"], (0.6, 0.4, 0.0, 0.0)
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertTrue(ensemble["deadline_fallback"])
        validate_classification_enrichment(pooled)

    def test_incomplete_non_fallback_pool_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not complete"):
            pool_classification_evidence(
                (evidence(1, (0.8, 0.1, 0.05, 0.05)),),
                deadline_fallback=False,
            )

    def test_decision_basis_is_an_exact_independently_addressed_copy(self):
        pooled = pool_classification_evidence(
            (evidence(1, (0.05, 0.05, 0.85, 0.05)),),
            deadline_fallback=True,
        )

        basis = classification_decision_basis(pooled)

        self.assertEqual(basis.kind, CLASSIFICATION_DECISION_BASIS)
        self.assertEqual(basis.value, pooled.value)
        self.assertEqual(basis.confidence, pooled.confidence)
        self.assertEqual(basis.result_id, "decision-basis:run:1:model")
        validate_classification_enrichment(basis)


if __name__ == "__main__":
    unittest.main()
