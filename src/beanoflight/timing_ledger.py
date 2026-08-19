"""Per-bean critical-path timing and counterfactual notice analysis."""

from __future__ import annotations

from collections.abc import Iterable

from .registry_models import BeanRecord
from .telemetry import summarize_samples

SHADOW_NOTICE_STEPS_MS = (5, 10, 15, 20, 25, 30, 50)


def bean_timing_ledger(record: BeanRecord) -> dict[str, object]:
    """Materialize one auditable ledger from timestamps already on the record."""

    job = record.inference_jobs[0] if record.inference_jobs else None
    decision = record.decision
    marks: dict[str, int] = {}
    if job is not None:
        marks.update(job.timing_marks_ns)
    if decision is not None:
        marks.update(decision.timing_marks_ns)

    durations: dict[str, float] = {}
    _source_duration(
        durations,
        "first_detection_to_crop_capture_ms",
        marks,
        "first_detection_source_ns",
        "crop_capture_source_ns",
    )
    classification = next(
        (
            enrichment
            for enrichment in reversed(record.enrichments)
            if enrichment.kind == "classification"
        ),
        None,
    )
    if classification is not None:
        first_detection = marks.get("first_detection_source_ns")
        crop_capture = marks.get("crop_capture_source_ns")
        if first_detection is not None:
            durations["first_detection_to_classification_ms"] = max(
                0, classification.timestamp_ns - first_detection
            ) / 1_000_000.0
        if crop_capture is not None:
            durations["crop_capture_to_classification_ms"] = max(
                0, classification.timestamp_ns - crop_capture
            ) / 1_000_000.0
    sorter_observed = marks.get("sorter_observed_source_ns")
    first_detection = marks.get("first_detection_source_ns")
    if sorter_observed is not None and first_detection is not None:
        durations["first_detection_to_sorter_ms"] = max(
            0, sorter_observed - first_detection
        ) / 1_000_000.0
    _duration(
        durations,
        "crop_selected_to_dispatch_ms",
        marks,
        "crop_selected_monotonic_ns",
        "dispatch_dequeued_monotonic_ns",
    )
    _duration(
        durations,
        "dispatch_to_inference_receive_ms",
        marks,
        "dispatch_dequeued_monotonic_ns",
        "inference_received_monotonic_ns",
    )
    _duration(
        durations,
        "inference_queue_ms",
        marks,
        "inference_received_monotonic_ns",
        "inference_started_monotonic_ns",
    )
    _duration(
        durations,
        "inference_service_ms",
        marks,
        "inference_started_monotonic_ns",
        "inference_completed_monotonic_ns",
    )
    _duration(
        durations,
        "classification_registry_to_sorter_ms",
        marks,
        "registry_classification_received_monotonic_ns",
        "sorter_event_received_monotonic_ns",
    )
    _duration(
        durations,
        "inference_complete_to_sorter_ms",
        marks,
        "inference_completed_monotonic_ns",
        "sorter_event_received_monotonic_ns",
    )

    reason = "" if decision is None else decision.reason
    late_by_ns = max(0, marks.get("additional_notice_required_ns", 0))
    result = "awaiting_decision"
    if decision is not None:
        if "too late" in reason:
            result = "too_late"
        elif "no gate" in reason:
            result = "no_gate"
        elif decision.gate_indices:
            result = "scheduled"
        else:
            result = "not_required"

    return {
        "bean_id": str(record.bean_ref),
        "sequence": record.bean_ref.sequence,
        "result": result,
        "decision_reason": reason,
        "frame_index": None if job is None else job.frame_index,
        "source_crop_px": (
            None
            if job is None
            else [
                job.source_crop_width_px or job.crop_width_px,
                job.source_crop_height_px or job.crop_height_px,
            ]
        ),
        "resized_crop": False if job is None else job.resized,
        "gate_indices": [] if decision is None else list(decision.gate_indices),
        "available_notice_ms": marks.get("available_notice_ns", 0) / 1_000_000.0,
        "late_by_ms": late_by_ns / 1_000_000.0,
        "equivalent_line_extension_mm": _line_extension_mm(record, late_by_ns),
        "durations_ms": durations,
        "marks_ns": marks,
    }


def summarize_timing_ledgers(
    records: Iterable[BeanRecord],
) -> dict[str, object]:
    ledgers = tuple(
        bean_timing_ledger(record)
        for record in records
        if record.inference_jobs or record.decision is not None
    )
    late = tuple(item for item in ledgers if item["result"] == "too_late")
    late_ms = [float(item["late_by_ms"]) for item in late]
    line_mm = [float(item["equivalent_line_extension_mm"]) for item in late]
    duration_names = sorted(
        {
            name
            for item in ledgers
            for name in item["durations_ms"]
        }
    )
    return {
        "schema": "beanoflight-timing-ledger/v1",
        "beans": len(ledgers),
        "resized_crops": sum(bool(item["resized_crop"]) for item in ledgers),
        "results": {
            name: sum(item["result"] == name for item in ledgers)
            for name in (
                "scheduled",
                "too_late",
                "no_gate",
                "not_required",
                "awaiting_decision",
            )
        },
        "late_by_ms": summarize_samples(late_ms),
        "equivalent_line_extension_mm": summarize_samples(line_mm),
        "shadow_recovered_with_extra_notice": {
            str(extra_ms): sum(float(item["late_by_ms"]) <= extra_ms for item in late)
            for extra_ms in SHADOW_NOTICE_STEPS_MS
        },
        "durations_ms": {
            name: summarize_samples(
                [
                    float(item["durations_ms"][name])
                    for item in ledgers
                    if name in item["durations_ms"]
                ]
            )
            for name in duration_names
        },
        "per_bean": list(ledgers),
    }


def _duration(
    output: dict[str, float],
    name: str,
    marks: dict[str, int],
    start: str,
    finish: str,
) -> None:
    if start in marks and finish in marks:
        output[name] = max(0, marks[finish] - marks[start]) / 1_000_000.0


def _source_duration(
    output: dict[str, float],
    name: str,
    marks: dict[str, int],
    start: str,
    finish: str,
) -> None:
    _duration(output, name, marks, start, finish)


def _line_extension_mm(record: BeanRecord, additional_notice_ns: int) -> float:
    prediction = record.prediction
    if prediction is None or additional_notice_ns <= 0:
        return 0.0
    seconds = additional_notice_ns / 1_000_000_000.0
    crossing_velocity = record.track.state[3] + (
        9_810.0 * prediction.seconds_until_crossing
    )
    return max(0.0, crossing_velocity * seconds + 0.5 * 9_810.0 * seconds**2)
