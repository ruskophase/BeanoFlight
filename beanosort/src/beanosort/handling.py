"""Manual material handling, count-aware predictions and immutable corrections."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from .auth import require_role
from .collections import check_destination_contents, eligible_destination
from .models import (Assignment, Container, Correction, Destination, HandlingPlan, Input, Lineage, Measurement,
                     Operation, Output, Pack, Portion, ProductLot, Profile, Reconciliation, Reservation, Station,
                     User, Version, identity, now)
from .quantities import DomainError, mass
from .service import active_assignment, audit, available, balance, empty_container, get, measure, post, remember, replay


def latest_profile(s, portion_id):
    return s.scalar(select(Profile).filter_by(portion_id=portion_id).order_by(Profile.created_at.desc(), Profile.version.desc()))


def distribution(data):
    if "distributions" in data:
        return data["distributions"]
    stream = data.get("applies_to")
    denominator = Decimal(data.get("expected", {}).get(stream, "0"))
    if denominator <= 0:
        return {}
    return {k: {label: str(Decimal(value) / denominator) for label, value in bins.items()}
            for k, bins in data.get("histograms", {}).get(stream, {}).items()}


def inherited(data, *, parent_profile, representative, operation_id, output_mg):
    density = data.get("count_per_kg") if representative else None
    return {"schema": "beanosort-population-profile/v2", "coverage": "unknown", "composition_evidence": "inherited_estimate",
            "distributions": distribution(data) if representative else {}, "count_per_kg": density,
            "estimated_count": str(Decimal(density) * output_mg / 1000000) if density else None,
            "source_profile_id": parent_profile, "source_evidence": data.get("composition_evidence", "unknown"),
            "comparability_key": data.get("comparability_key"), "representative_withdrawal_assumed": representative,
            "count_density_evidence": data.get("count_density_evidence", "unknown") if representative else "unknown",
            "histogram_definitions": data.get("histogram_definitions", {}), "basis": "estimated_particle_count",
            "denominator": "estimated count of this portion; fractions may overlap for multi-label properties",
            "dependence_group": operation_id, "note": "Complementary portions share the parent uncertainty; observed counts are not duplicated. Package-level uniformity is not established.",
            "moisture": {"value": None, "evidence": "unknown"}}


def predict_blend(ingredients):
    """Count weighting requires an explicit count-per-mass estimate for EVERY input."""
    total_mass = sum(item["mass_mg"] for item in ingredients)
    result = {"schema": "beanosort-population-profile/v2", "composition_evidence": "calculated_prediction", "coverage": "unknown",
              "mass_contributions": [{"portion_id": i["portion_id"], "mass_mg": i["mass_mg"],
                                      "mass_fraction": str(Decimal(i["mass_mg"]) / total_mass), "profile_id": i.get("profile_id")} for i in ingredients],
              "distributions": {}, "count_per_kg": None, "estimated_count": None,
              "basis": "estimated_particle_count", "denominator": "sum of estimated ingredient counts",
              "histogram_definitions": {}, "ingredient_evidence": [{"profile_id": i.get("profile_id"),
                  "composition_evidence": i["profile"].get("composition_evidence", "unknown"),
                  "count_density_evidence": i["profile"].get("count_density_evidence", "unknown")} for i in ingredients],
              "moisture": {"value": None, "evidence": "unknown"}, "mixing_uniformity": "not established"}
    keys = {i["profile"].get("comparability_key") for i in ingredients}
    if None in keys or len(keys) != 1:
        result["note"] = "Cannot establish comparable population measurements across ingredients."
        return result
    if any(not i["profile"].get("count_per_kg") for i in ingredients):
        result["note"] = "Count-based composition unknown: every ingredient needs a justified count-per-mass estimate."
        return result
    counts = [Decimal(i["profile"]["count_per_kg"]) * i["mass_mg"] / 1000000 for i in ingredients]
    total_count = sum(counts)
    result.update(estimated_count=str(total_count), count_per_kg=str(total_count * 1000000 / total_mass), comparability_key=next(iter(keys)))
    distributions = [distribution(i["profile"]) for i in ingredients]
    common = set.intersection(*(set(d) for d in distributions))
    for feature in sorted(common):
        definitions = [d["profile"].get("histogram_definitions", {}).get(feature) for d in ingredients]
        if any(definition != definitions[0] for definition in definitions):
            continue  # Feature schemas are not interchangeable merely because keys match.
        result["histogram_definitions"][feature] = definitions[0]
        labels = set.union(*(set(d[feature]) for d in distributions))
        result["distributions"][feature] = {label: str(sum(n * Decimal(d[feature].get(label, "0")) for n, d in zip(counts, distributions)) / total_count) for label in sorted(labels)}
    result["note"] = "Prediction assumes representative ingredients. Counts weight count-based properties; physical mixing and package uniformity are not verified."
    return result


def validate_specification(kind, data):
    if not isinstance(data, dict):
        raise DomainError("Specification must be an object")
    if kind == "packaging_specification":
        if set(data) - {"sku", "nominal_mg", "description"}:
            raise DomainError("Unsupported packaging constraint; it cannot be silently ignored")
        if not data.get("sku") or type(data.get("nominal_mg")) is not int or data["nominal_mg"] <= 0:
            raise DomainError("Packaging specification requires SKU and positive integer nominal_mg")
    elif kind == "product_specification":
        if set(data) - {"constraints", "description"} or not isinstance(data.get("constraints", []), list):
            raise DomainError("Unsupported product constraints; this version evaluates count fractions only")
        for c in data.get("constraints", []):
            if not isinstance(c, dict) or set(c) - {"feature", "category", "basis", "unit", "minimum", "maximum", "required_evidence"}:
                raise DomainError("Unsupported product constraint; it cannot be silently ignored")
            if not isinstance(c.get("required_evidence", []), list):
                raise DomainError("required_evidence must be a list of evidence kinds")
            if c.get("basis") != "count" or c.get("unit") != "fraction" or not c.get("feature") or not c.get("category"):
                raise DomainError("Implemented constraints require feature/category, count basis and fraction units")
            for field in ("minimum", "maximum"):
                try:
                    value = Decimal(str(c.get(field, "0" if field == "minimum" else "1")))
                except InvalidOperation:
                    raise DomainError("Specification fractions must be decimal numbers") from None
                if not value.is_finite() or not 0 <= value <= 1:
                    raise DomainError("Specification fractions must be between zero and one")
            if Decimal(str(c.get("minimum", "0"))) > Decimal(str(c.get("maximum", "1"))):
                raise DomainError("Specification minimum exceeds maximum")
    else:
        raise DomainError("Unsupported specification type")


def evaluate_specification(profile, specification):
    results = []
    for constraint in specification.get("constraints", []):
        bins = distribution(profile).get(constraint["feature"])
        value = bins.get(constraint["category"]) if bins else None
        unknown = not bins or Decimal(bins.get("unknown", "0")) > 0 or value is None
        required = constraint.get("required_evidence", [])
        if required and profile.get("composition_evidence") not in required:
            unknown = True
        status = "unknown" if unknown else "prediction_pass" if Decimal(str(constraint.get("minimum", "0"))) <= Decimal(value) <= Decimal(str(constraint.get("maximum", "1"))) else "prediction_fail"
        results.append({"constraint": constraint, "value": value, "status": status})
    overall = "prediction_fail" if any(r["status"] == "prediction_fail" for r in results) else "unknown" if not results or any(r["status"] == "unknown" for r in results) else "prediction_pass"
    return {"status": overall, "verified_compliance": False, "constraints": results}


def effective_reconciliation(s, op):
    correction = s.scalar(select(Correction).filter_by(original_operation_id=op.id).order_by(Correction.version.desc()))
    return correction or s.scalar(select(Reconciliation).filter_by(operation_id=op.id))


def effective_outputs(s, op):
    recon = effective_reconciliation(s, op)
    owner = recon.operation_id if isinstance(recon, Correction) else op.id
    return list(s.scalars(select(Output).filter_by(operation_id=owner)))


def new_operation(s, user, kind, station_id, amount, environment, policy_id=None):
    get(s, Station, station_id)
    op = Operation(id=identity("op"), kind=kind, status="prepared", station_id=station_id, operator_id=user.id,
                   boundary_mg=amount, planned_mg=amount, policy_id=policy_id,
                   execution_method="simulated" if environment == "simulation" else "manual")
    s.add(op)
    s.flush()
    return op


def consume(s, user, op, portion):
    amount = balance(s, portion.id)
    s.add(Input(operation_id=op.id, portion_id=portion.id, mass_mg=amount))
    post(s, op, portion, -amount, op.kind + " input consumed", user)
    portion.status = "consumed"
    assignment = active_assignment(s, portion_id=portion.id)
    if assignment:
        assignment.active, assignment.ended_at = False, now()
    s.flush()
    return assignment


def produce(s, user, op, stream, amount, container_id, parents, profile, *, existing_mass_mg=None, portion_id=None):
    if not amount:
        return None
    if container_id:
        container = get(s, Container, container_id)
        limit = container.capacity_mg
        if limit is not None and existing_mass_mg is not None:
            limit = max(limit, existing_mass_mg)
        if active_assignment(s, container_id=container_id) or (limit is not None and amount > limit):
            raise DomainError("Output container occupied or capacity exceeded")
    portion = Portion(id=portion_id or identity("portion"), created_by=op.id, status="stock" if container_id else "wip",
                      quality="held", station_id=op.station_id if not container_id else None)
    s.add(portion)
    s.flush()
    for parent in parents:
        s.add(Lineage(parent_id=parent.id, child_id=portion.id, operation_id=op.id))
    post(s, op, portion, amount, op.kind + " " + stream, user)
    s.add(Output(operation_id=op.id, portion_id=portion.id, stream=stream, mass_mg=amount))
    if container_id:
        s.add(Assignment(id=identity("assignment"), container_id=container_id, portion_id=portion.id, operation_id=op.id))
    s.add(Profile(id=identity("profile"), operation_id=op.id, portion_id=portion.id, stream=stream, version=1, data=profile))
    return portion


class MaterialHandling:
    def __init__(self, db):
        self.db = db

    def prepare(self, user_id, key, kind, data):
        if kind not in {"transfer", "split", "blend", "packaging"}:
            raise DomainError("Unknown manual operation")
        payload = {"kind": kind, "data": data, "operator": user_id}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            ids = list(data["portion_ids"])
            if len(set(ids)) != len(ids) or (kind != "blend" and len(ids) != 1):
                raise DomainError("Choose one source, or at least two distinct whole portions for blending")
            destination = get(s, Container, data["container_id"])
            assigned = None
            if kind == "blend":
                if not eligible_destination(s, destination):
                    raise DomainError("Blend destination must be empty or contain released, unreserved bulk material")
                assigned = active_assignment(s, container_id=destination.id)
                existing_id = assigned.portion_id if assigned else ""
                if "destination_portion_id" in data and data["destination_portion_id"] != existing_id:
                    raise DomainError("Destination contents changed; reload the combine form and review its contents")
                if assigned and assigned.portion_id not in ids:
                    ids.append(assigned.portion_id)
                if len(ids) < 2:
                    raise DomainError("Choose at least two distinct whole portions for blending, including any existing destination contents")
            ingredients = []
            for pid in ids:
                portion = get(s, Portion, pid)
                amount = balance(s, pid)
                if available(s, portion) != amount or amount <= 0 or portion.hold_reason or not active_assignment(s, portion_id=pid):
                    raise DomainError("Every source must be released, unreserved stock in a container")
                if s.scalar(select(ProductLot.id).filter_by(portion_id=pid)):
                    raise DomainError("Opened-pack rework requires a separate future operation")
                p = latest_profile(s, pid)
                ingredients.append({"portion_id": pid, "mass_mg": amount, "profile_id": p.id if p else None,
                                    "profile": p.data if p else {}})
            amount = sum(i["mass_mg"] for i in ingredients)
            if not data.get("ready"):
                raise DomainError("Confirm the destination and its existing contents are ready for combining" if kind == "blend"
                                  else "Confirm the destination is physically empty, clean and ready")
            if kind == "blend":
                if destination.capacity_mg is not None and amount > destination.capacity_mg:
                    raise DomainError("Combined input mass exceeds destination container capacity")
            else:
                empty_container(s, destination.id)
            if get(s, Version, data["policy_id"]).kind != "reconciliation_policy":
                raise DomainError("Select a reconciliation policy")
            spec = get(s, Version, data["specification_id"]) if data.get("specification_id") else None
            if kind == "packaging" and (not spec or spec.kind != "packaging_specification"):
                raise DomainError("Packaging requires an exact packaging specification version")
            if kind == "blend" and spec and spec.kind != "product_specification":
                raise DomainError("Blending requires a product specification")
            op = new_operation(s, user, kind, data["station_id"], amount, self.db.environment, data["policy_id"])
            plan = {"ingredients": ingredients, "destination_id": data["container_id"],
                    "prediction": predict_blend(ingredients) if kind == "blend" else None,
                    "requested_mass_mg": mass(data["withdrawn_mass"]) if kind == "split" else amount}
            readiness = {"ready_to_receive": True, "operator_id": user.id, "at": now(),
                         "container_revision": destination.revision}
            if assigned:
                existing = next(i for i in ingredients if i["portion_id"] == assigned.portion_id)
                readiness.update(existing_portion_id=assigned.portion_id, existing_assignment_id=assigned.id,
                                 existing_mass_mg=existing["mass_mg"], existing_profile_id=existing["profile_id"])
                plan.update(destination_portion_id=assigned.portion_id, destination_starting_mass_mg=existing["mass_mg"])
            else:
                readiness["empty_clean_ready"] = True
            if kind == "split" and not 0 < plan["requested_mass_mg"] < amount:
                raise DomainError("A split needs a positive withdrawn quantity smaller than the whole portion")
            if spec and kind == "blend":
                plan["specification_evaluation"] = evaluate_specification(plan["prediction"], spec.data)
            s.add(HandlingPlan(id=identity("plan"), operation_id=op.id, specification_id=spec.id if spec else None, data=plan))
            for item in ingredients:
                s.add(Reservation(id=identity("reservation"), operation_id=op.id, portion_id=item["portion_id"], mass_mg=item["mass_mg"]))
                s.add(Input(operation_id=op.id, portion_id=item["portion_id"], mass_mg=item["mass_mg"]))
            s.flush()  # The occupied-destination guard requires its reservation and input to exist.
            s.add(Destination(id=identity("destination"), operation_id=op.id, container_id=data["container_id"], stream="output",
                              readiness=readiness))
            audit(s, user, "manual_operation_prepared", {"kind": kind, "plan": plan}, op)
            remember(s, key, payload, op.id)
            return op.id

    def start(self, user_id, operation_id):
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            op = get(s, Operation, operation_id)
            if op.status == "running":
                return
            if op.status != "prepared" or not s.scalar(select(HandlingPlan.id).filter_by(operation_id=op.id)):
                raise DomainError("Prepare a manual operation first")
            if op.kind == "blend":
                check_destination_contents(s, op, start=True)
            for item in s.scalars(select(Input).filter_by(operation_id=op.id)):
                portion = get(s, Portion, item.portion_id)
                if portion.quality != "released" or portion.hold_reason:
                    raise DomainError("Held input cannot be processed")
                portion.status = "wip"
            op.status = "running"
            audit(s, user, "manual_work_started", {"hardware_action": "none"}, op)

    def execute(self, user_id, key, operation_id, data):
        payload = {"command": "execute_material", "operation": operation_id, "operator": user_id, "data": data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            op = get(s, Operation, operation_id)
            if op.status not in {"running", "paused", "failed", "finishing"} or s.scalar(select(Reconciliation.id).filter_by(operation_id=op.id)):
                raise DomainError("Start a manual operation; it can be accounted only once")
            plan = s.scalar(select(HandlingPlan).filter_by(operation_id=op.id))
            if not plan or not data.get("physical_confirmation") or not data.get("reason", "").strip():
                raise DomainError("Confirm actual physical work and record its method/reason")
            inputs = list(s.scalars(select(Input).filter_by(operation_id=op.id)))
            parents = [get(s, Portion, i.portion_id) for i in inputs]
            if any(p.hold_reason for p in parents):
                raise DomainError("Resolve the input quality hold before execution")
            if op.kind == "blend":
                check_destination_contents(s, op)
            dest = plan.data["destination_id"]
            if op.kind == "transfer":
                original = active_assignment(s, portion_id=parents[0].id)
                container = get(s, Container, dest)
                if not original or active_assignment(s, container_id=dest) or (container.capacity_mg and op.boundary_mg > container.capacity_mg):
                    raise DomainError("Transfer destination unavailable or too small")
                original.active, original.ended_at = False, now()
                s.flush()
                s.add(Assignment(id=identity("assignment"), container_id=dest, portion_id=parents[0].id, operation_id=op.id))
                parents[0].status = "stock"
                s.add(Output(operation_id=op.id, portion_id=parents[0].id, stream="transferred", mass_mg=op.boundary_mg))
                op.status, op.release_status = "completed", "released"
            else:
                loss = mass(data.get("loss", ""), zero=True)
                remainder = mass(data.get("remainder", ""), zero=True) if op.kind in {"split", "packaging"} else 0
                packs = [mass(x) for x in data.get("pack_masses", [])] if op.kind == "packaging" else []
                if op.kind == "packaging" and not 1 <= len(packs) <= 10000:
                    raise DomainError("Enter 1–10000 actual net pack weights; nominal weights are not measurements")
                output_mass = sum(packs) if packs else mass(data.get("output_mass", ""))
                discrepancy = op.boundary_mg - output_mass - remainder - loss
                if op.kind == "split" and not remainder:
                    raise DomainError("Split must leave a positive remainder; use transfer for a whole portion")
                actual_inputs = data.get("actual_inputs", {})
                if op.kind == "blend" and any(actual_inputs.get(i.portion_id) in (None, "")
                                              or mass(actual_inputs[i.portion_id]) != i.mass_mg for i in inputs):
                    raise DomainError("Record actual contributions matching the reserved portions; split or correct them before preparing another plan")
                original_container = active_assignment(s, portion_id=parents[0].id).container_id
                for parent, item in zip(parents, inputs):
                    if balance(s, parent.id) != item.mass_mg:
                        raise DomainError("Input balance changed")
                    post(s, op, parent, -item.mass_mg, op.kind + " consumed", user)
                    parent.status = "consumed"
                    a = active_assignment(s, portion_id=parent.id)
                    a.active, a.ended_at = False, now()
                    if op.kind == "blend":
                        measure(s, op, "input_" + str(inputs.index(item)), item.mass_mg, data["evidence"], user, self.db.environment)
                s.flush()
                representative = bool(data.get("representative"))
                ingredient = plan.data["ingredients"][0]
                if op.kind == "blend":
                    predicted = plan.data["prediction"]
                    profile = {**predicted, "mixing_method": data["reason"], "actual_contributions": actual_inputs,
                               "specification_evaluation": plan.data.get("specification_evaluation", {"status": "unknown"})}
                    if loss and not representative:
                        profile.update(distributions={}, count_per_kg=None, estimated_count=None,
                                       note="Loss composition unknown; input prediction cannot establish output composition")
                    elif profile.get("count_per_kg"):
                        profile["estimated_count"] = str(Decimal(profile["count_per_kg"]) * output_mass / 1000000)
                    if discrepancy:
                        profile.update(distributions={}, count_per_kg=None, estimated_count=None,
                                       note="Unexplained mass difference; input prediction does not establish output composition")
                    if plan.specification_id:
                        profile["specification_evaluation"] = evaluate_specification(profile, get(s, Version, plan.specification_id).data)
                else:
                    profile = inherited(ingredient["profile"], parent_profile=ingredient["profile_id"], representative=representative,
                                        operation_id=op.id, output_mg=output_mass)
                output = produce(s, user, op, "product" if packs else "withdrawn" if op.kind == "split" else "blend",
                                 output_mass, dest, parents, profile)
                if remainder:
                    remainder_profile = inherited(ingredient["profile"], parent_profile=ingredient["profile_id"], representative=representative,
                                                  operation_id=op.id, output_mg=remainder)
                    produce(s, user, op, "remainder", remainder, original_container, parents, remainder_profile,
                            existing_mass_mg=inputs[0].mass_mg)
                for role, value in (("output", output_mass), ("remainder", remainder), ("loss", loss)):
                    measure(s, op, role, value, data["evidence"], user, self.db.environment)
                if packs:
                    spec = get(s, Version, plan.specification_id)
                    lot = ProductLot(id=identity("product"), operation_id=op.id, portion_id=output.id, specification_id=spec.id,
                                     sku=spec.data["sku"], nominal_mg=spec.data["nominal_mg"])
                    s.add(lot)
                    s.flush()
                    for n, value in enumerate(packs, 1):
                        measure(s, op, "pack_" + str(n), value, data["evidence"], user, self.db.environment)
                        s.add(Pack(lot_id=lot.id, ordinal=n, actual_net_mg=value,
                                   evidence={**data["evidence"], "operator_id": user.id, "recorded_at": now(), "quantity_basis": "actual net bean mass"}))
                s.add(Reconciliation(id=identity("reconciliation"), operation_id=op.id, discrepancy_mg=discrepancy,
                      accounting={"input_mg": op.boundary_mg, "output_mg": output_mass, "remainder_mg": remainder, "loss_mg": loss},
                      reason=data["reason"], policy_id=op.policy_id))
                op.status, op.release_status = "awaiting_reconciliation", "held"
            for model in (Reservation, Destination):
                for r in s.scalars(select(model).filter_by(operation_id=op.id, active=True)):
                    r.active = False
            audit(s, user, "manual_work_recorded", {"reason": data["reason"], "kind": op.kind, "physical_confirmation": True}, op)
            remember(s, key, payload, op.id)
            return op.id

    def count_sample(self, user_id, key, portion_id, sample_count, sample_mass, data):
        payload = {"kind": "count_sample", "portion": portion_id, "count": sample_count, "mass": sample_mass, "data": data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            portion = get(s, Portion, portion_id)
            if s.scalar(select(ProductLot.id).filter_by(portion_id=portion_id)):
                raise DomainError("Opened-pack sampling requires a separate rework operation")
            if available(s, portion) != balance(s, portion_id) or available(s, portion) <= 0:
                raise DomainError("Profile only unreserved, released material")
            if type(sample_count) is not int or sample_count <= 0 or not data.get("representative") or not data.get("sample_returned"):
                raise DomainError("Positive count, representativeness assumption and sample-return confirmation are required")
            sample_mg = mass(sample_mass)
            if sample_mg > balance(s, portion_id):
                raise DomainError("Sample exceeds the portion")
            parent = latest_profile(s, portion_id)
            profile = deepcopy(parent.data) if parent else {"coverage": "unknown", "composition_evidence": "unknown", "distributions": {}}
            density = Decimal(sample_count) * 1000000 / sample_mg
            profile.update(count_per_kg=str(density), estimated_count=str(density * balance(s, portion_id) / 1000000),
                           count_density_evidence={"kind": "calculated_prediction", "sample_count": sample_count, "sample_mass_mg": sample_mg,
                                                   "representative_assumption": True, "sample_returned": True, "provenance": data["evidence"]},
                           source_profile_id=parent.id if parent else None)
            if self.db.environment == "simulation":
                profile["comparability_key"] = "synthetic-fixture/v1"
            if profile.get("retained_population"):
                from .retained_populations import enrich_profile
                profile = enrich_profile(s, profile, portion_id, balance(s, portion_id), original=parent)
            # Unknown real calibration is never made comparable by this count sample.
            op = new_operation(s, user, "profiling", data["station_id"], balance(s, portion_id), self.db.environment)
            op.status, op.release_status = "completed", "released"
            s.add(Input(operation_id=op.id, portion_id=portion_id, mass_mg=balance(s, portion_id)))
            measure(s, op, "returned_sample", sample_mg, data["evidence"], user, self.db.environment)
            record = Profile(id=identity("profile"), operation_id=op.id, portion_id=portion_id, stream="sample", version=1, data=profile)
            s.add(record)
            audit(s, user, "count_sample_recorded", {"portion_id": portion_id, "profile_id": record.id, "sample_returned": True}, op)
            remember(s, key, payload, record.id)
            return record.id

    def clear_hold(self, user_id, portion_id, reason):
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "supervisor", "admin")
            portion = get(s, Portion, portion_id)
            if not reason.strip() or not portion.hold_reason:
                raise DomainError("An explicit hold and reviewed disposition reason are required")
            previous = portion.hold_reason
            portion.hold_reason = None
            owner = get(s, Operation, portion.created_by)
            # Removing a quality hold never bypasses reconciliation/release.
            portion.quality = "released" if owner.release_status == "released" and portion.status != "wip" else "held"
            audit(s, user, "quality_hold_cleared", {"portion_id": portion_id, "previous_reason": previous, "disposition": reason}, owner)

    def correct_sorting(self, user_id, key, operation_id, data):
        payload = {"command": "correct_sorting", "operation": operation_id, "data": data, "operator": user_id}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            original = get(s, Operation, operation_id)
            from .segments import current
            if current(s,original):
                raise DomainError("Multi-collection measurements require their own reviewed adjustment; review all weights before accepting reconciliation")
            from .collections import starting_mass
            if starting_mass(s, original):
                raise DomainError("Occupied-destination measurements require a reviewed adjustment; the single-input correction form is not applicable")
            if original.kind != "sorting" or original.release_status != "held" or not effective_reconciliation(s, original):
                raise DomainError("Only reconciled, unreleased sorting outputs can be corrected")
            if not data.get("reason", "").strip():
                raise DomainError("A correction needs a reviewed reason")
            quantities = {k: mass(data.get(k, ""), zero=True) for k in ("selected", "pass_through", "remainder", "retained", "loss", "sample")}
            outputs = effective_outputs(s, original)
            if not outputs:
                raise DomainError("No output population exists to correct; use a reviewed rework boundary")
            if any(quantities[k] > 0 and k not in {o.stream for o in outputs} for k in ("selected", "pass_through", "remainder", "retained")):
                raise DomainError("Adding a previously absent stream requires explicit rework, not a measurement correction")
            if any(s.scalar(select(Reservation.id).filter_by(portion_id=o.portion_id, active=True)) for o in outputs):
                raise DomainError("Corrected outputs must be unreserved")
            op = new_operation(s, user, "adjustment", original.station_id, sum(o.mass_mg for o in outputs), self.db.environment, original.policy_id)
            for output in outputs:
                parent = get(s, Portion, output.portion_id)
                if parent.status == "consumed" or parent.hold_reason or balance(s, parent.id) != output.mass_mg:
                    raise DomainError("Outputs have changed or have an explicit hold; cannot correct this boundary")
                profile = latest_profile(s, parent.id)
                assignment = consume(s, user, op, parent)
                produce(s, user, op, output.stream, quantities[output.stream], assignment.container_id if assignment else None,
                        [parent], {**(profile.data if profile else {}), "measurement_correction_of": parent.id,
                                   "note": "Population evidence unchanged; mass corrected with a superseding record."})
            for role, amount in quantities.items():
                measure(s, op, role, amount, data["evidence"], user, self.db.environment)
            version = (s.scalar(select(func.max(Correction.version)).filter_by(original_operation_id=original.id)) or 0) + 1
            correction = Correction(id=identity("correction"), original_operation_id=original.id, operation_id=op.id, version=version,
                                    accounting={"input_mg": original.boundary_mg, **{k + "_mg": v for k, v in quantities.items()}},
                                    discrepancy_mg=original.boundary_mg - sum(quantities.values()), reason=data["reason"])
            s.add(correction)
            op.status, op.release_status = "awaiting_reconciliation", "held"
            audit(s, user, "measurements_superseded", {"original_operation_id": original.id, "correction_id": correction.id, "reason": data["reason"]}, original)
            remember(s, key, payload, correction.id)
            return correction.id
