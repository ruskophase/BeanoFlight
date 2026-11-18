"""Pure capture assumptions and fixtures. No tracker, classifier or hardware IO."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Protocol

from .quantities import DomainError


FEATURE_SCHEMA = "beanosort-compact-particle/v1"
CAPTURE_DEFAULT = {
    "algorithm": "independent-capture-sha256/v1", "probability": "0.97",
    "arrival_delay_ms": 100, "timeout_ms": 200, "timing_evidence": "simulation",
    "assumptions": ["one independent trial per attempted tracked particle",
                    "misses reach pass-through", "intentional pass reaches pass-through",
                    "no neighbour capture, spill, retention, multiple capture or detector errors",
                    "same probability for all targets"],
}
FEATURES = {
    "shape": {"unit": "category", "categories": ["round", "elongated", "irregular", "unknown"]},
    "brightness": {"unit": "fixture intensity 0..255", "edges": [0, 85, 170, 256], "intervals": "[lower,upper)"},
    "volume_proxy_mm3": {"unit": "mm³ proxy, not mass", "edges": [0, 100, 200, 1000000], "intervals": "[lower,upper)"},
    "defects": {"unit": "category", "multilabel": True},
    "shape_brightness": {"unit": "joint category", "definition": "shape × brightness bin"},
    "lab_l": {"unit": "CIE L*", "edges": [0, 20, 40, 60, 80, 101], "intervals": "[lower,upper)"},
    "lab_chroma": {"unit": "CIE chroma", "edges": [0, 5, 10, 20, 50, 1000], "intervals": "[lower,upper)"},
    "size_mm": {"unit": "major axis mm", "edges": [0, 5, 10, 15, 20, 1000], "intervals": "[lower,upper)"},
    "classification_category": {"unit": "source model category; not necessarily defect"},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def probability(model):
    try:
        p = Decimal(str(model["probability"]))
        if not p.is_finite() or not 0 <= p <= 1:
            raise ValueError()
    except (KeyError, ValueError, InvalidOperation):
        raise DomainError("Capture probability must be between 0 and 1") from None
    if model.get("algorithm") != "independent-capture-sha256/v1":
        raise DomainError("Unsupported capture model version")
    if not 0 <= model.get("arrival_delay_ms", -1) < model.get("timeout_ms", -1):
        raise DomainError("Synthetic arrival delay must precede timeout")
    return p


def simulated_destination(run_id, particle_key, seed, targeted, model):
    p = probability(model)
    if not targeted:
        return "pass_through"
    # The hash is a stateless random draw, not an exact proportion allocator.
    # Stable namespaced identities make retry and processing order irrelevant.
    draw = int(digest([model["algorithm"], str(seed), run_id, particle_key]), 16)
    numerator, denominator = p.as_integer_ratio()
    return "selected" if draw * denominator < numerator * 2**256 else "pass_through"


@dataclass(frozen=True)
class CaptureEvent:
    kind: str
    evidence: str
    data: dict


class CaptureMonitor(Protocol):
    def events(self, destination: str, model: dict) -> list[CaptureEvent]: ...


class SimulatedMonitor:
    def events(self, destination, model):
        probability(model)
        result = [CaptureEvent("expected_arrival", "calculated_prediction", {
            "delay_ms": model["arrival_delay_ms"], "clock": "synthetic_relative_ms",
            "timeout_ms": model["timeout_ms"], "timing_evidence": "simulation"})]
        if destination == "selected":
            result.extend([
                CaptureEvent("chute_detection", "simulation", {"chute": "selected", "offset_ms": model["arrival_delay_ms"]}),
                CaptureEvent("association", "simulation", {"strength": "synthetic_identity_known", "physical_confirmation": False}),
            ])
        else:
            result.append(CaptureEvent("timeout_unconfirmed", "simulation", {
                "offset_ms": model["timeout_ms"], "synthetic_truth": "miss", "physical_confirmation": False}))
        return result


# Future monitor adapters may emit ambiguous_association, detector_fault,
# detector_unavailable and timeout_unconfirmed. An arrival never by itself
# proves target identity. No physical monitor implementation ships in Phase 1.


def validate_particle(row, environment):
    if not isinstance(row, dict):
        raise DomainError("Each particle must be a compact JSON object")
    if row.get("schema") != FEATURE_SCHEMA or not isinstance(row.get("key"), str) or not row["key"] or len(row["key"]) > 200:
        raise DomainError("Invalid particle identity/schema")
    if type(row.get("sequence")) is not int or row["sequence"] < 0:
        raise DomainError("Particle source sequence must be a nonnegative integer")
    if row.get("decision") not in {"selected", "pass", "unknown"}:
        raise DomainError("Invalid decision")
    if row.get("action") not in {"attempted", "not_issued", "unknown"}:
        raise DomainError("Invalid action record")
    if row["decision"] != "selected" and row["action"] == "attempted":
        raise DomainError("Attempt without a selection decision requires adapter review")
    evidence = row.get("evidence", {})
    if not isinstance(evidence, dict) or not all(isinstance(x, dict) for x in evidence.values()):
        raise DomainError("Evidence aspects must be JSON objects")
    if not all(evidence.get(x) for x in ("features", "decision", "action")):
        raise DomainError("Each feature/decision/action needs evidence provenance")
    allowed = {"simulation"} if environment == "simulation" else {"image_derived", "model_derived", "controller_record", "unknown", "simulation"}
    if any(item.get("kind") not in allowed for item in evidence.values()) or (environment == "real" and evidence["features"].get("kind") == "simulation"):
        raise DomainError("Synthetic and real upstream particle evidence cannot cross environments")
    if not all(item.get("source") and item.get("version") for item in evidence.values()):
        raise DomainError("Evidence needs source and version")
    if row.get("source_at") is not None:
        try:
            stamp = datetime.fromisoformat(row["source_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise DomainError("source_at must be an offset-aware timestamp or null; retain device clocks separately") from None
    if environment == "real":
        if row["action"] == "attempted" and evidence["action"]["kind"] != "controller_record":
            raise DomainError("A real capture trial requires a reported controller action")
        if row["decision"] != "unknown" and evidence["decision"]["kind"] not in {"model_derived", "controller_record"}:
            raise DomainError("A known decision requires model or controller evidence")
    features = row.get("features")
    if not isinstance(features, dict):
        raise DomainError("Missing compact features")
    if features.get("shape") not in {None, "round", "elongated", "irregular", "unknown"}:
        raise DomainError("Unknown shape category; adapt explicitly")
    for key, upper in (("brightness", 256), ("volume_proxy_mm3", 1000000), ("lab_l", 101), ("lab_chroma", 1000), ("size_mm", 1000), ("area_mm2", 1000000)):
        if features.get(key) is not None:
            try:
                value = Decimal(str(features[key]))
                if not value.is_finite() or not 0 <= value < upper:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                raise DomainError(f"Invalid {key}") from None
    defects = features.get("defects")
    if defects is not None and (not isinstance(defects, list) or not all(isinstance(x, str) and x for x in defects)):
        raise DomainError("Defects must be a list, or null for unknown")
    digest(row)  # rejects non-JSON/non-finite values before persistence


def fixture_particle(sequence):
    """Explicit synthetic upstream fixture; never an independent image classifier."""
    return {
        "schema": FEATURE_SCHEMA, "key": str(sequence), "sequence": sequence,
        "source_at": None, "decision": "selected" if sequence % 5 == 0 else "pass",
        "action": "attempted" if sequence % 5 == 0 else "not_issued",
        "frames_observed": 3, "valve_count": 2 if sequence % 10 == 0 else 1,
        "features": {"shape": ("round", "elongated", "irregular")[sequence % 3],
                     "brightness": (sequence * 37) % 256,
                     "volume_proxy_mm3": str(50 + sequence * 7 % 250),
                     "defects": ["broken"] if sequence % 5 == 0 else []},
        "evidence": {key: {"kind": "simulation", "source": "fixture-generator", "version": "v1"}
                     for key in ("features", "decision", "action")},
    }


def categories(features):
    shape = features.get("shape") or "unknown"
    bins = {}
    for key in ("brightness", "volume_proxy_mm3", "lab_l", "lab_chroma", "size_mm"):
        value = features.get(key)
        bins[key] = "unknown"
        if value is not None:
            edges = FEATURES[key]["edges"]
            for lower, upper in zip(edges, edges[1:]):
                if lower <= Decimal(str(value)) < upper:
                    bins[key] = f"[{lower},{upper})"
                    break
    defects = features.get("defects")
    return {"shape": [shape], **{k: [v] for k, v in bins.items()},
            "classification_category": [features.get("classification_category") or "unknown"],
            "defects": ["unknown"] if defects is None else sorted(set(defects)) or ["none"],
            "shape_brightness": [f"{shape} × {bins['brightness']}"]}


def expected_profile(particles, model, *, synthetic=False, coverage="unknown"):
    p = probability(model)
    totals = {x: Decimal(0) for x in ("selected", "pass_through", "unresolved")}
    histograms = {x: {key: defaultdict(Decimal) for key in FEATURES} for x in totals}
    counts = {"tracked": 0, "selected_decisions": 0, "intentionally_passed": 0,
              "action_attempted": 0, "decision_action_discrepancies": 0,
              "physical_confirmed": 0, "ambiguous": 0, "unconfirmed": 0}
    evidence_kinds = {key: set() for key in ("features", "classification", "decision", "action")}
    synthetic_counts = {"selected": 0, "pass_through": 0, "timeouts": 0}
    synthetic_hist = {x: {key: defaultdict(int) for key in FEATURES} for x in ("selected", "pass_through")}
    for item in particles:
        row = item["payload"]
        counts["tracked"] += 1
        counts["selected_decisions"] += row["decision"] == "selected"
        weights = {x: Decimal(0) for x in totals}
        if row["decision"] == "selected" and row["action"] == "attempted":
            counts["action_attempted"] += 1
            counts["unconfirmed"] += 1  # a controller command is not a chute observation
            weights.update(selected=p, pass_through=1 - p)
        elif row["decision"] == "pass":
            counts["intentionally_passed"] += 1
            weights["pass_through"] = Decimal(1)
        else:
            weights["unresolved"] = Decimal(1)
            counts["decision_action_discrepancies"] += row["decision"] == "selected"
        cats = categories(row["features"])
        for aspect, evidence in row["evidence"].items():
            if aspect in evidence_kinds:
                evidence_kinds[aspect].add(evidence["kind"])
        for stream, weight in weights.items():
            totals[stream] += weight
            for key, values in cats.items():
                for value in values:
                    histograms[stream][key][value] += weight
        destination = item.get("simulated_destination")
        if destination:
            synthetic_counts[destination] += 1
            synthetic_counts["timeouts"] += row["decision"] == "selected" and destination == "pass_through"
            for key, values in cats.items():
                for value in values:
                    synthetic_hist[destination][key][value] += 1
    return {
        "schema": "beanosort-population-profile/v1", "basis": "tracked_particle_count",
        "coverage": coverage, "physical_census": False, "estimated_physical_count": None,
        "moisture": {"value": None, "evidence": "unknown"},
        "comparability": "unknown outside this feature/configuration version",
        "composition_evidence": "calculated_prediction", "upstream_synthetic": synthetic,
        "aspect_evidence": {k: sorted(v) for k, v in evidence_kinds.items()},
        "capture_model": model, "counts": counts,
        "expected": {k: str(v) for k, v in totals.items()},
        "histogram_definitions": FEATURES, "denominator": "expected count for each stream; unresolved retained separately",
        "histograms": {s: {k: {c: str(v) for c, v in bins.items()} for k, bins in h.items()} for s, h in histograms.items()},
        "synthetic_monitor": {"evidence": "simulation", "counts": synthetic_counts,
                              "histograms": {s: {k: dict(v) for k, v in h.items()} for s, h in synthetic_hist.items()}},
        "compliance": "cannot establish compliance; this sorting profile has no product specification evaluation",
    }
