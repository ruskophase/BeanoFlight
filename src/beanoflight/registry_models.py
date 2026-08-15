"""Immutable BeanRegistry records and their versioned wire representation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import (
    BeanEvent,
    BeanRef,
    CrossingPrediction,
    Detection,
    Gate,
    GateProbability,
    Observation,
    TrackSnapshot,
    TrackStatus,
)

REGISTRY_SCHEMA = "beanoflight-registry/v1"


@dataclass(frozen=True, slots=True)
class Enrichment:
    """A versioned result produced by a worker other than the tracker."""

    source: str
    kind: str
    value: Any
    timestamp_ns: int
    version: str = ""
    result_id: str = ""
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SortingDecision:
    """A proposed or acknowledged gate action for one bean."""

    decision_id: str
    source: str
    timestamp_ns: int
    actuation_timestamp_ns: int
    gate_indices: tuple[int, ...]
    policy_version: str = ""
    reason: str = ""
    acknowledged_timestamp_ns: int | None = None


@dataclass(frozen=True, slots=True)
class BeanRecord:
    """Materialized current state owned by BeanRegistry."""

    bean_ref: BeanRef
    revision: int
    status: TrackStatus
    created_timestamp_ns: int
    updated_timestamp_ns: int
    track: TrackSnapshot
    prediction: CrossingPrediction | None = None
    enrichments: tuple[Enrichment, ...] = ()
    decision: SortingDecision | None = None


def bean_ref_to_dict(bean_ref: BeanRef) -> dict[str, object]:
    return {"run_id": bean_ref.run_id, "sequence": bean_ref.sequence}


def bean_ref_from_dict(value: Mapping[str, object]) -> BeanRef:
    return BeanRef(str(value["run_id"]), int(value["sequence"]))


def detection_to_dict(detection: Detection) -> dict[str, object]:
    return {
        "centroid_px": list(detection.centroid_px),
        "bbox_px": list(detection.bbox_px),
        "area_px": detection.area_px,
        "solidity": detection.solidity,
        "mean_bgr": list(detection.mean_bgr),
    }


def detection_from_dict(value: Mapping[str, object]) -> Detection:
    centroid = _float_tuple(value["centroid_px"], 2)
    bbox = _int_tuple(value["bbox_px"], 4)
    mean_bgr = _float_tuple(value["mean_bgr"], 3)
    return Detection(
        centroid_px=(centroid[0], centroid[1]),
        bbox_px=(bbox[0], bbox[1], bbox[2], bbox[3]),
        area_px=int(value["area_px"]),
        solidity=float(value["solidity"]),
        mean_bgr=(mean_bgr[0], mean_bgr[1], mean_bgr[2]),
    )


def observation_to_dict(observation: Observation) -> dict[str, object]:
    return {
        "frame_index": observation.frame_index,
        "timestamp_ns": observation.timestamp_ns,
        "detection": detection_to_dict(observation.detection),
        "position_mm": list(observation.position_mm),
    }


def observation_from_dict(value: Mapping[str, object]) -> Observation:
    position = _float_tuple(value["position_mm"], 2)
    return Observation(
        frame_index=int(value["frame_index"]),
        timestamp_ns=int(value["timestamp_ns"]),
        detection=detection_from_dict(_mapping(value["detection"])),
        position_mm=(position[0], position[1]),
    )


def track_to_dict(
    track: TrackSnapshot, *, include_history: bool = True
) -> dict[str, object]:
    return {
        "bean_ref": bean_ref_to_dict(track.bean_ref),
        "status": track.status.value,
        "timestamp_ns": track.timestamp_ns,
        "state": list(track.state),
        "covariance": [list(row) for row in track.covariance],
        "hits": track.hits,
        "misses": track.misses,
        "last_bbox_px": list(track.last_bbox_px),
        "history": (
            [observation_to_dict(item) for item in track.history]
            if include_history
            else []
        ),
    }


def track_from_dict(value: Mapping[str, object]) -> TrackSnapshot:
    state = _float_tuple(value["state"], 4)
    covariance_value = _sequence(value["covariance"])
    if len(covariance_value) != 4:
        raise ValueError("track covariance must contain four rows")
    covariance = tuple(_float_tuple(row, 4) for row in covariance_value)
    bbox = _int_tuple(value["last_bbox_px"], 4)
    history = tuple(
        observation_from_dict(_mapping(item))
        for item in _sequence(value.get("history", []))
    )
    return TrackSnapshot(
        bean_ref=bean_ref_from_dict(_mapping(value["bean_ref"])),
        status=TrackStatus(str(value["status"])),
        timestamp_ns=int(value["timestamp_ns"]),
        state=(state[0], state[1], state[2], state[3]),
        covariance=covariance,
        hits=int(value["hits"]),
        misses=int(value["misses"]),
        last_bbox_px=(bbox[0], bbox[1], bbox[2], bbox[3]),
        history=history,
    )


def prediction_to_dict(prediction: CrossingPrediction) -> dict[str, object]:
    return {
        "bean_ref": bean_ref_to_dict(prediction.bean_ref),
        "line_y_mm": prediction.line_y_mm,
        "crossing_timestamp_ns": prediction.crossing_timestamp_ns,
        "seconds_until_crossing": prediction.seconds_until_crossing,
        "x_mean_mm": prediction.x_mean_mm,
        "x_std_mm": prediction.x_std_mm,
        "time_std_ms": prediction.time_std_ms,
        "gates": [
            {
                "index": item.gate.index,
                "left_mm": item.gate.left_mm,
                "right_mm": item.gate.right_mm,
                "probability": item.probability,
            }
            for item in prediction.gates
        ],
        "selected_gate_indices": list(prediction.selected_gate_indices),
    }


def prediction_from_dict(value: Mapping[str, object]) -> CrossingPrediction:
    gates = tuple(
        GateProbability(
            gate=Gate(
                index=int(item["index"]),
                left_mm=float(item["left_mm"]),
                right_mm=float(item["right_mm"]),
            ),
            probability=float(item["probability"]),
        )
        for item in (_mapping(raw) for raw in _sequence(value["gates"]))
    )
    return CrossingPrediction(
        bean_ref=bean_ref_from_dict(_mapping(value["bean_ref"])),
        line_y_mm=float(value["line_y_mm"]),
        crossing_timestamp_ns=int(value["crossing_timestamp_ns"]),
        seconds_until_crossing=float(value["seconds_until_crossing"]),
        x_mean_mm=float(value["x_mean_mm"]),
        x_std_mm=float(value["x_std_mm"]),
        time_std_ms=float(value["time_std_ms"]),
        gates=gates,
        selected_gate_indices=_int_tuple(value["selected_gate_indices"]),
    )


def enrichment_to_dict(enrichment: Enrichment) -> dict[str, object]:
    return {
        "source": enrichment.source,
        "kind": enrichment.kind,
        "value": enrichment.value,
        "timestamp_ns": enrichment.timestamp_ns,
        "version": enrichment.version,
        "result_id": enrichment.result_id,
        "confidence": enrichment.confidence,
    }


def enrichment_from_dict(value: Mapping[str, object]) -> Enrichment:
    confidence = value.get("confidence")
    return Enrichment(
        source=str(value["source"]),
        kind=str(value["kind"]),
        value=value.get("value"),
        timestamp_ns=int(value["timestamp_ns"]),
        version=str(value.get("version", "")),
        result_id=str(value.get("result_id", "")),
        confidence=None if confidence is None else float(confidence),
    )


def decision_to_dict(decision: SortingDecision) -> dict[str, object]:
    return {
        "decision_id": decision.decision_id,
        "source": decision.source,
        "timestamp_ns": decision.timestamp_ns,
        "actuation_timestamp_ns": decision.actuation_timestamp_ns,
        "gate_indices": list(decision.gate_indices),
        "policy_version": decision.policy_version,
        "reason": decision.reason,
        "acknowledged_timestamp_ns": decision.acknowledged_timestamp_ns,
    }


def decision_from_dict(value: Mapping[str, object]) -> SortingDecision:
    acknowledgement = value.get("acknowledged_timestamp_ns")
    return SortingDecision(
        decision_id=str(value["decision_id"]),
        source=str(value["source"]),
        timestamp_ns=int(value["timestamp_ns"]),
        actuation_timestamp_ns=int(value["actuation_timestamp_ns"]),
        gate_indices=_int_tuple(value["gate_indices"]),
        policy_version=str(value.get("policy_version", "")),
        reason=str(value.get("reason", "")),
        acknowledged_timestamp_ns=(
            None if acknowledgement is None else int(acknowledgement)
        ),
    )


def record_to_dict(
    record: BeanRecord, *, include_history: bool = True
) -> dict[str, object]:
    return {
        "bean_ref": bean_ref_to_dict(record.bean_ref),
        "revision": record.revision,
        "status": record.status.value,
        "created_timestamp_ns": record.created_timestamp_ns,
        "updated_timestamp_ns": record.updated_timestamp_ns,
        "track": track_to_dict(record.track, include_history=include_history),
        "prediction": (
            None if record.prediction is None else prediction_to_dict(record.prediction)
        ),
        "enrichments": [enrichment_to_dict(item) for item in record.enrichments],
        "decision": None if record.decision is None else decision_to_dict(record.decision),
    }


def record_from_dict(value: Mapping[str, object]) -> BeanRecord:
    prediction_value = value.get("prediction")
    decision_value = value.get("decision")
    track = track_from_dict(_mapping(value["track"]))
    bean_ref = bean_ref_from_dict(_mapping(value["bean_ref"]))
    if track.bean_ref != bean_ref:
        raise ValueError("record and track bean references differ")
    prediction = (
        None
        if prediction_value is None
        else prediction_from_dict(_mapping(prediction_value))
    )
    if prediction is not None and prediction.bean_ref != bean_ref:
        raise ValueError("record and prediction bean references differ")
    return BeanRecord(
        bean_ref=bean_ref,
        revision=int(value["revision"]),
        status=TrackStatus(str(value["status"])),
        created_timestamp_ns=int(value["created_timestamp_ns"]),
        updated_timestamp_ns=int(value["updated_timestamp_ns"]),
        track=track,
        prediction=prediction,
        enrichments=tuple(
            enrichment_from_dict(_mapping(item))
            for item in _sequence(value.get("enrichments", []))
        ),
        decision=(
            None if decision_value is None else decision_from_dict(_mapping(decision_value))
        ),
    )


def event_to_dict(event: BeanEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "bean_ref": bean_ref_to_dict(event.bean_ref),
        "timestamp_ns": event.timestamp_ns,
        "revision": event.revision,
        "stream_sequence": event.stream_sequence,
        "payload": event.payload,
    }


def event_from_dict(value: Mapping[str, object]) -> BeanEvent:
    payload = value.get("payload", {})
    return BeanEvent(
        kind=str(value["kind"]),
        bean_ref=bean_ref_from_dict(_mapping(value["bean_ref"])),
        timestamp_ns=int(value["timestamp_ns"]),
        payload=dict(_mapping(payload)),
        revision=int(value.get("revision", 0)),
        event_id=str(value.get("event_id", "")),
        stream_sequence=int(value.get("stream_sequence", 0)),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("registry value must be an object")
    return value


def _sequence(value: object) -> list[object] | tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("registry value must be an array")
    return value


def _float_tuple(value: object, length: int) -> tuple[float, ...]:
    sequence = _sequence(value)
    if len(sequence) != length:
        raise ValueError(f"registry array must contain {length} values")
    return tuple(float(item) for item in sequence)


def _int_tuple(value: object, length: int | None = None) -> tuple[int, ...]:
    sequence = _sequence(value)
    if length is not None and len(sequence) != length:
        raise ValueError(f"registry array must contain {length} values")
    return tuple(int(item) for item in sequence)
