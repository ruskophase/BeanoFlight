"""Classification evidence validation and deterministic ensemble pooling."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from .registry_models import Enrichment

CLASSIFICATION_EVIDENCE = "classification_evidence"
CLASSIFICATION_POOLED = "classification_pooled"
CLASSIFICATION_DECISION_BASIS = "classification_decision_basis"
LEGACY_CLASSIFICATION = "classification"
MEAN_PROBABILITY_POOLING_VERSION = "mean-probability-v1"


def classification_ensemble_id(enrichment: Enrichment) -> str:
    value = enrichment.value
    if not isinstance(value, Mapping):
        return ""
    ensemble = value.get("ensemble")
    if not isinstance(ensemble, Mapping):
        return ""
    return str(ensemble.get("id", ""))


def evidence_for_ensemble(
    enrichments: Iterable[Enrichment], ensemble_id: str | None = None
) -> tuple[Enrichment, ...]:
    evidence = tuple(
        item
        for item in enrichments
        if item.kind == CLASSIFICATION_EVIDENCE
        and (ensemble_id is None or classification_ensemble_id(item) == ensemble_id)
    )
    if ensemble_id is not None:
        return _unique_ordered_evidence(evidence)
    if not evidence:
        return ()
    selected_id = classification_ensemble_id(evidence[-1])
    return _unique_ordered_evidence(
        item for item in evidence if classification_ensemble_id(item) == selected_id
    )


def pooled_for_ensemble(
    enrichments: Iterable[Enrichment], ensemble_id: str | None = None
) -> Enrichment | None:
    candidates = tuple(
        item
        for item in enrichments
        if item.kind == CLASSIFICATION_POOLED
        and (ensemble_id is None or classification_ensemble_id(item) == ensemble_id)
    )
    return candidates[-1] if candidates else None


def expected_evidence_count(evidence: Sequence[Enrichment]) -> int:
    if not evidence:
        return 0
    value = _mapping(evidence[0].value, "classification evidence value")
    ensemble = _mapping(value.get("ensemble"), "classification ensemble")
    return int(ensemble.get("expected_samples", 1))


def pool_ready_classification(
    enrichments: Iterable[Enrichment],
) -> Enrichment | None:
    evidence = evidence_for_ensemble(enrichments)
    if not evidence:
        return None
    ensemble_id = classification_ensemble_id(evidence[0])
    existing = pooled_for_ensemble(enrichments, ensemble_id)
    if existing is not None:
        return existing
    expected = expected_evidence_count(evidence)
    if len(evidence) < expected:
        return None
    return pool_classification_evidence(evidence[:expected], deadline_fallback=False)


def pool_classification_evidence(
    evidence: Sequence[Enrichment],
    *,
    deadline_fallback: bool,
    timestamp_ns: int | None = None,
) -> Enrichment:
    if not evidence:
        raise ValueError("classification pooling requires at least one result")
    evidence = _unique_ordered_evidence(evidence)
    first_value = _mapping(evidence[0].value, "classification evidence value")
    classes = _class_order(first_value)
    ensemble_id = classification_ensemble_id(evidence[0])
    if not ensemble_id:
        raise ValueError("classification evidence requires an ensemble ID")
    expected = expected_evidence_count(evidence)
    vectors = []
    for item in evidence:
        value = _mapping(item.value, "classification evidence value")
        if classification_ensemble_id(item) != ensemble_id:
            raise ValueError("classification evidence belongs to different ensembles")
        if _class_order(value) != classes:
            raise ValueError("classification class order changed within an ensemble")
        if expected_evidence_count((item,)) != expected:
            raise ValueError("expected sample count changed within an ensemble")
        vectors.append(_probability_vector(value, len(classes)))
    if len(vectors) > expected:
        raise ValueError("classification ensemble contains too many samples")
    if not deadline_fallback and len(vectors) < expected:
        raise ValueError("classification ensemble is not complete")

    means = [
        sum(vector[index] for vector in vectors) / len(vectors)
        for index in range(len(classes))
    ]
    total = sum(means)
    probabilities = [value / total for value in means]
    winner = max(range(len(classes)), key=probabilities.__getitem__)
    member_ids = [item.result_id for item in evidence]
    result_timestamp = max(item.timestamp_ns for item in evidence)
    if timestamp_ns is not None:
        result_timestamp = max(result_timestamp, int(timestamp_ns))
    if not deadline_fallback:
        method = MEAN_PROBABILITY_POOLING_VERSION
    elif len(vectors) == 1:
        method = "deadline-single-evidence"
    else:
        method = "deadline-mean-probability-v1"
    return Enrichment(
        source=(
            "beano-sorter-deadline"
            if deadline_fallback
            else "bean-registry-ensemble"
        ),
        kind=CLASSIFICATION_POOLED,
        value={
            "category": classes[winner],
            "class_order": list(classes),
            "probabilities": probabilities,
            "ensemble": {
                "id": ensemble_id,
                "expected_samples": expected,
                "sample_count": len(vectors),
                "member_result_ids": member_ids,
                "pooling_method": method,
                "deadline_fallback": deadline_fallback,
            },
        },
        timestamp_ns=result_timestamp,
        version=MEAN_PROBABILITY_POOLING_VERSION,
        result_id=f"pooled:{ensemble_id}",
        confidence=probabilities[winner],
    )


def classification_decision_basis(classification: Enrichment) -> Enrichment:
    """Return an immutable audit copy of the exact pool used by a decision."""

    if classification.kind != CLASSIFICATION_POOLED:
        raise ValueError("classification decision basis requires a pooled result")
    ensemble_id = classification_ensemble_id(classification)
    if not ensemble_id:
        raise ValueError("classification decision basis requires an ensemble ID")
    return replace(
        classification,
        source="beano-sorter",
        kind=CLASSIFICATION_DECISION_BASIS,
        result_id=f"decision-basis:{ensemble_id}",
    )


def validate_classification_enrichment(enrichment: Enrichment) -> None:
    if enrichment.kind not in {
        CLASSIFICATION_EVIDENCE,
        CLASSIFICATION_POOLED,
        CLASSIFICATION_DECISION_BASIS,
    }:
        return
    value = _mapping(enrichment.value, "classification value")
    classes = _class_order(value)
    probabilities = _probability_vector(value, len(classes))
    ensemble = _mapping(value.get("ensemble"), "classification ensemble")
    if not str(ensemble.get("id", "")).strip():
        raise ValueError("classification ensemble ID is required")
    expected = int(ensemble.get("expected_samples", 0))
    if not 1 <= expected <= 5:
        raise ValueError("classification expected samples must be between 1 and 5")
    if enrichment.kind == CLASSIFICATION_EVIDENCE:
        sample_index = int(ensemble.get("sample_index", 0))
        if not 1 <= sample_index <= expected:
            raise ValueError("classification sample index is outside the ensemble")
        logits = value.get("logits")
        if logits is not None:
            raw_logits = _sequence(logits, "classification logits")
            if len(raw_logits) != len(classes) or any(
                not math.isfinite(float(item)) for item in raw_logits
            ):
                raise ValueError("classification logits must match the class order")
    else:
        sample_count = int(ensemble.get("sample_count", 0))
        fallback = bool(ensemble.get("deadline_fallback", False))
        if sample_count <= 0 or sample_count > expected:
            raise ValueError("classification pooled sample count is invalid")
        if sample_count < expected and not fallback:
            raise ValueError("an incomplete classification pool must be a fallback")
    category = str(value.get("category", ""))
    if category != classes[max(range(len(classes)), key=probabilities.__getitem__)]:
        raise ValueError("classification category does not match its probability vector")


def _unique_ordered_evidence(
    evidence: Iterable[Enrichment],
) -> tuple[Enrichment, ...]:
    by_index: dict[int, Enrichment] = {}
    for item in evidence:
        value = _mapping(item.value, "classification evidence value")
        ensemble = _mapping(value.get("ensemble"), "classification ensemble")
        index = int(ensemble.get("sample_index", 0))
        by_index.setdefault(index, item)
    return tuple(by_index[index] for index in sorted(by_index))


def _class_order(value: Mapping[str, object]) -> tuple[str, ...]:
    classes = tuple(str(item) for item in _sequence(value.get("class_order"), "class order"))
    if not classes or any(not item.strip() for item in classes):
        raise ValueError("classification class order cannot be empty")
    if len(set(classes)) != len(classes):
        raise ValueError("classification class order contains duplicates")
    return classes


def _probability_vector(
    value: Mapping[str, object], expected_length: int
) -> tuple[float, ...]:
    probabilities = tuple(
        float(item)
        for item in _sequence(value.get("probabilities"), "classification probabilities")
    )
    if len(probabilities) != expected_length:
        raise ValueError("classification probabilities must match the class order")
    if any(not math.isfinite(item) or not 0 <= item <= 1 for item in probabilities):
        raise ValueError("classification probabilities must be finite values from 0 to 1")
    if not math.isclose(sum(probabilities), 1.0, rel_tol=0, abs_tol=1e-6):
        raise ValueError("classification probabilities must sum to one")
    return probabilities


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    return value
