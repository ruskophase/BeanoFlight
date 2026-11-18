"""Read-only histogram interval inspection, grouped by archived reference bean.

Expected contributors are weighted possibilities, never per-bean capture claims.
SQL grouping keeps large synthetic runs out of browser payloads and ORM memory.
"""
from decimal import Decimal
import math

from sqlalchemy import func, select

from .capture import probability
from .models import Particle
from .quantities import DomainError
from .references import features, load_dataset, matches, recipe_definitions

PAGE_SIZE = 24


def contribution(population, basis, decision, action, destination, p):
    if population == "input":
        return Decimal(1)
    if basis == "simulated":
        return Decimal(int(destination == population))
    if decision == "selected" and action == "attempted":
        return p if population == "selected" else 1-p
    return Decimal(int(population == "pass_through" and decision == "pass"))


def colour_samples(bean):
    result = {}
    for camera, prefix in (("CamL", "caml"), ("CamR", "camr")):
        values = [bean.get(f"{prefix}_mean_{channel}_median") for channel in "rgb"]
        # Use the same recorded mean RGB fields as the statistics dashboard.
        # Missing colour is unknown; do not invent a grey or infer it from Lab.
        result[camera] = [int(math.floor(v+.5)) for v in values] if all(
            type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 255 for v in values) else None
    return result


def inspect_population(session, dataset, recipe, model, *, feature, bucket,
                       population="input", basis="expected", page=1, run_id=None, collection=None, sort_collection=None):
    definitions = recipe_definitions(dataset, recipe)
    if feature not in definitions or population not in {"input", "selected", "pass_through"} or basis not in {"expected", "simulated"}:
        raise DomainError("Choose a known chart, population and evidence basis")
    edges = definitions[feature]["edges"]
    if type(bucket) is not int or not 0 <= bucket < len(edges)-1 or type(page) is not int or not 1 <= page <= 100000:
        raise DomainError("Choose a valid histogram interval and page")
    if (run_id is None or collection) and basis == "simulated":
        raise DomainError("A reference preview has no simulated destination records")
    rows, snapshot = load_dataset(dataset)
    by_sequence = {row["bean_sequence"]: row for row in rows}
    lo, hi = edges[bucket:bucket+2]
    p = probability(model)
    if collection:
        from .collections import collection_members
        groups=[]
        for seq,count in collection_members(collection,population):
            value=features(by_sequence[seq],definitions)[feature]
            if value is not None and lo<=value<hi:groups.append((seq,value,"unknown","unknown",None,count))
    elif run_id is None:
        groups = []
        for row in rows:
            values = features(row, definitions)
            value = values[feature]
            if value is None or not lo <= value < hi:
                continue
            selected = matches(values[recipe["feature"]], recipe)
            groups.append((row["bean_sequence"], value,
                           "unknown" if selected is None else "selected" if selected else "pass",
                           "attempted" if selected else "not_issued", None, 1))
    else:
        sequence = Particle.payload["reference_bean"]["bean_sequence"].as_integer()
        value = Particle.payload["features"][feature].as_float()
        decision = Particle.payload["decision"].as_string()
        action = Particle.payload["action"].as_string()
        query=select(sequence,value,decision,action,Particle.simulated_destination,func.count(Particle.id)).where(Particle.run_id==run_id,value>=lo,value<hi)
        if sort_collection:
            from .segments import particle_filter
            condition=particle_filter(session,sort_collection)
            if condition is None:raise DomainError("This collection has no reliable local particle association")
            query=query.where(condition)
        groups = session.execute(query.group_by(sequence, value, decision, action, Particle.simulated_destination)).all()
    beans = {}
    for seq, value, decision, action, destination, count in groups:
        weight = Decimal(1) if collection else contribution(population, basis, decision, action, destination, p)
        if not weight:
            continue
        if seq not in by_sequence:
            raise DomainError("Run evidence references an unavailable archived bean")
        entry = beans.setdefault(seq, {"bean_sequence": seq, "bean_id": by_sequence[seq]["bean_id"],
                                      "feature_value": value, "count": Decimal(0), "recorded_passages": 0,
                                      "missed_target_passages": 0})
        entry["count"] += weight*count
        entry["recorded_passages"] += count if not collection else 0
        if basis == "simulated" and destination == "pass_through" and decision == "selected":
            entry["missed_target_passages"] += count
    total = sum((entry["count"] for entry in beans.values()), Decimal(0))
    pages = max(1, math.ceil(len(beans)/PAGE_SIZE))
    page = min(page, pages)
    chosen = sorted(beans.values(), key=lambda row: row["bean_sequence"])[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    for entry in chosen:
        bean = by_sequence[entry["bean_sequence"]]
        entry["count"] = str(entry["count"])
        entry["colours"] = colour_samples(bean)
        entry["photos"] = {camera: f"/reference-datasets/{dataset.id}/photos/{entry['bean_sequence']}/{camera}"
            for camera in ("CamL", "CamR") if bean.get("photo_"+camera) in snapshot["photo_hashes"]}
    return {"schema": "beanosort-population-inspection/v1", "dataset_id": dataset.id,
            "dataset_fingerprint": dataset.fingerprint, "feature": feature, "title": definitions[feature]["title"],
            "unit": definitions[feature]["unit"], "bucket": bucket, "lower": lo, "upper": hi,
            "interval": "[lower,upper)", "population": population, "basis": basis,
            "capture_probability": str(p), "run_id": run_id, "matching_count": str(total),
            "reference_bean_count": len(beans), "page": page, "pages": pages, "page_size": PAGE_SIZE,
            "physical_confirmation": False, "collection":bool(collection),
            "count_unit":"estimated beans" if collection else None, "photo_evidence": "archived source images, not chute verification",
            "membership_evidence": "calculated_prediction" if collection or (basis == "expected" and population != "input") else "simulation" if run_id else "reference sample",
            "count_basis": "estimated beans" if collection else "count-weighted expected contributors" if basis == "expected" and population != "input" else "recorded simulated passages" if run_id else "distinct reference beans",
            "items": chosen}
