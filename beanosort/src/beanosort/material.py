"""Atomic contents movements, final-product withdrawals and weight adjustments."""
from copy import deepcopy
from decimal import Decimal

from sqlalchemy import select

from .auth import require_role
from .handling import inherited, latest_profile, new_operation, predict_blend, produce as produce_material
from .models import (Assignment, HandlingPlan, Input, Output, Portion,
                     ProductLot, Profile, Station, User, identity, now)
from .quantities import DomainError, mass
from .service import active_assignment, audit, balance, container_usage, get, measure, post, remember, replay

SCHEMA = "beanosort-material-record/v1"
KINDS = {"contents_move", "weight_adjustment", "product_packaging"}


def produce(session, user, op, stream, amount, container_id, parents, profile, **kwargs):
    from .retained_populations import enrich_profile
    pid = identity("portion")
    profile = enrich_profile(session, profile, pid, amount)
    return produce_material(session, user, op, stream, amount, container_id, parents, profile, portion_id=pid, **kwargs)


def container_state(session, container):
    assignment = active_assignment(session, container_id=container.id)
    portion = get(session, Portion, assignment.portion_id) if assignment else None
    profile = latest_profile(session, portion.id) if portion else None
    return {"container_id": container.id, "label": container.label, "location": container.location,
            "revision": container.revision, "capacity_mg": container.capacity_mg, "tare_mg": container.tare_mg,
            "archived": container.archived, "deleted": container.deleted,
            "assignment_id": assignment.id if assignment else None,
            "portion_id": portion.id if portion else None, "mass_mg": balance(session, portion.id) if portion else 0,
            "status": portion.status if portion else None, "quality": portion.quality if portion else None,
            "hold_reason": portion.hold_reason if portion else None, "profile_id": profile.id if profile else None,
            "packed": bool(portion and session.scalar(select(ProductLot.id).filter_by(portion_id=portion.id))),
            "locked": container_usage(session, container)["locked"]}


def eligible(state, *, source=False):
    return (not any(state[key] for key in ("archived", "deleted", "packed", "locked"))
            and (not state["portion_id"] or state["status"] == "stock")
            and (not source or bool(state["portion_id"] and state["mass_mg"] > 0)))


def packaging_eligible(state):
    return eligible(state, source=True) and state["quality"] == "released" and not state["hold_reason"]


def _checked(session, container_id, expected, *, source=False):
    from .models import Container
    state = container_state(session, get(session, Container, container_id))
    if not isinstance(expected, dict) or state != expected:
        raise DomainError("Container contents or availability changed; reload the form before recording this work")
    if not eligible(state, source=source):
        raise DomainError("Choose available bulk material outside active jobs; archived, reserved and packaged containers cannot be used")
    return state


def _evidence(environment, basis="net after tare"):
    return {"kind": "simulation" if environment == "simulation" else "direct_measurement",
            "method": "Operator-entered scale reading", "tare_basis": basis}


def _weighing(state, data):
    reading = mass(data.get("weight"), zero=True)
    basis = data.get("weighing_basis")
    if basis not in {"gross", "net"}:
        raise DomainError("Choose a gross or net weighing")
    if basis == "gross" and (state["tare_mg"] is None or reading < state["tare_mg"]):
        raise DomainError("Gross weighing needs a recorded tare and cannot be less than that tare")
    return reading, basis, reading - state["tare_mg"] if basis == "gross" else reading


def _profile(session, state, amount, op, representative, *, enrich=True):
    original = session.get(Profile, state["profile_id"]) if state["profile_id"] else None
    data = original.data if original else {}
    result = inherited(data, parent_profile=state["profile_id"], representative=representative,
                       operation_id=op.id, output_mg=amount)
    if representative and not data.get("material_population_unknown"):
        for field in ("material_components", "combined_population", "retained_population"):
            if field in data:
                result[field] = deepcopy(data[field])
        if "combined_population" in result and state["mass_mg"]:
            pool = result["combined_population"]
            ratio = Decimal(amount) / state["mass_mg"]
            for field in ("before_count", "added_count", "combined_count"):
                if field in pool:
                    pool[field] = str(Decimal(pool[field]) * ratio)
    else:
        result["material_population_unknown"] = True
    result["note"] = ("Population estimates assume representative material; the weighing does not identify individual beans."
                      if representative else "Composition after this change is unknown; no representative-material assumption was accepted.")
    from .retained_populations import enrich_profile
    return enrich_profile(session, result, state["portion_id"], amount, original=original) if enrich else result


def _component(state, amount):
    return {"portion_id": state["portion_id"], "profile_id": state["profile_id"], "mass_mg": amount}


def _quality(portion, states):
    held = [state for state in states if state["quality"] == "held" or state["hold_reason"]]
    portion.quality = "held" if held else "released"
    portion.hold_reason = "; ".join(dict.fromkeys(state["hold_reason"] or "Inherited unreleased material" for state in held))[:2000] or None


def _consume(session, user, op, state):
    portion = get(session, Portion, state["portion_id"])
    session.add(Input(operation_id=op.id, portion_id=portion.id, mass_mg=state["mass_mg"]))
    post(session, op, portion, -state["mass_mg"], "Contents movement: original portion superseded", user)
    portion.status = "consumed"
    assignment = get(session, Assignment, state["assignment_id"])
    assignment.active, assignment.ended_at = False, now()


def _result(portion, container_id, amount, basis):
    return {"portion_id": portion.id if portion else None, "container_id": container_id,
            "mass_mg": amount, "mass_basis": basis, "quality": portion.quality if portion else None}


class MaterialActions:
    def __init__(self, db):
        self.db = db

    def _begin(self, session, user_id, key, kind, data):
        user = get(session, User, user_id)
        require_role(user, "operator", "supervisor", "admin")
        payload = {"command": kind, "operator": user_id, "data": data}
        previous = replay(session, key, payload)
        if not previous and data.get("confirmed") is not True:
            raise DomainError("Confirm that the physical work and any required weighing are complete")
        return user, payload, previous

    def _operation(self, session, user, kind, amount):
        if kind == "product_packaging":
            station = session.scalar(select(Station).filter_by(name="Packaging desk", kind="packaging"))
            if not station:
                raise DomainError("Register the Packaging desk by upgrading the database before recording final product")
        else:
            station = session.scalar(select(Station).where(Station.kind == "manual").order_by(Station.created_at, Station.id))
        if not station:
            raise DomainError("Register a manual station before recording material work")
        return new_operation(session, user, kind, station.id, amount, self.db.environment)

    def _finish(self, session, user, key, payload, op, record):
        op.status, op.release_status = "completed", "released"
        session.add(HandlingPlan(id=identity("plan"), operation_id=op.id, data={"schema": SCHEMA, **record}))
        audit(session, user, op.kind, record, op)
        remember(session, key, payload, op.id)
        return op.id

    def move_contents(self, user_id, key, data):
        with self.db.write() as session:
            user, payload, previous = self._begin(session, user_id, key, "contents_move", data)
            if previous:
                return previous
            if data.get("source_id") == data.get("destination_id"):
                raise DomainError("Choose different source and destination containers")
            source = _checked(session, data.get("source_id"), data.get("source_state"), source=True)
            destination = _checked(session, data.get("destination_id"), data.get("destination_state"))
            empty_source = data.get("empty_source") is True
            moved = source["mass_mg"] if empty_source else mass(data.get("moved_mass"))
            if not empty_source and moved >= source["mass_mg"]:
                raise DomainError("A partial move must leave material; select Empty the source for a complete movement")
            remainder = 0 if empty_source else source["mass_mg"] - moved
            difference = 0
            reason = str(data.get("reason") or "").strip()
            final = destination["mass_mg"] + moved
            if destination["capacity_mg"] is not None and final > destination["capacity_mg"]:
                raise DomainError("The resulting contents exceed the destination's nominal capacity")
            op = self._operation(session, user, "contents_move", max(source["mass_mg"] + destination["mass_mg"], final + remainder))
            representative = empty_source or data.get("representative") is True
            if not empty_source:
                measure(session, op, "moved", moved, _evidence(self.db.environment), user, self.db.environment)
            outputs = []
            if empty_source and not destination["portion_id"]:
                portion = get(session, Portion, source["portion_id"])
                session.add(Input(operation_id=op.id, portion_id=portion.id, mass_mg=source["mass_mg"]))
                assignment = get(session, Assignment, source["assignment_id"])
                assignment.active, assignment.ended_at = False, now()
                session.flush()
                session.add(Assignment(id=identity("assignment"), container_id=destination["container_id"], portion_id=portion.id, operation_id=op.id))
                session.add(Output(operation_id=op.id, portion_id=portion.id, stream="transferred", mass_mg=moved))
                outputs.append(_result(portion, destination["container_id"], moved, "entire recorded source balance; no new weighing"))
            else:
                parents = [get(session, Portion, source["portion_id"])]
                states = [source]
                _consume(session, user, op, source)
                if destination["portion_id"]:
                    parents.append(get(session, Portion, destination["portion_id"]))
                    states.append(destination)
                    _consume(session, user, op, destination)
                session.flush()
                if destination["portion_id"]:
                    ingredients = [{**_component(state, amount), "profile": session.get(Profile, state["profile_id"]).data if state["profile_id"] else {}}
                                   for state, amount in ((source, moved), (destination, destination["mass_mg"]))]
                    profile = predict_blend(ingredients)
                    profile["material_components"] = [_component(source, moved), _component(destination, destination["mass_mg"])]
                    if not representative or any(item["profile"].get("material_population_unknown") for item in ingredients):
                        profile.update(composition_evidence="unknown", distributions={}, count_per_kg=None, estimated_count=None,
                                       material_population_unknown=True, note="Composition after this combination is unknown; contributing source records remain in the ancestry.")
                else:
                    profile = _profile(session, source, moved, op, representative, enrich=False)
                    profile.pop("combined_population", None)
                    profile["material_components"] = [_component(source, moved)]
                profile["representative_withdrawal_assumed"] = representative
                output = produce(session, user, op, "combined" if destination["portion_id"] else "moved", final,
                                 destination["container_id"], parents, profile)
                _quality(output, states)
                outputs.append(_result(output, destination["container_id"], final,
                    "recorded destination contents plus entire recorded source balance" if empty_source else "recorded destination contents plus measured transfer"))
                if remainder:
                    profile = _profile(session, source, remainder, op, representative, enrich=False)
                    profile.pop("combined_population", None)
                    profile["material_components"] = [_component(source, remainder)]
                    output = produce(session, user, op, "remainder", remainder, source["container_id"], parents[:1], profile,
                                     existing_mass_mg=source["mass_mg"])
                    _quality(output, [source])
                    outputs.append(_result(output, source["container_id"], remainder, "calculated remainder; not separately weighed"))
            return self._finish(session, user, key, payload, op, {
                "source": source, "destination": destination, "destination_id": destination["container_id"],
                "moved_mg": moved, "source_after_mg": remainder, "destination_after_mg": final,
                "weight_adjustment_mg": difference, "empty_source": empty_source,
                "transfer_basis": "recorded_source_balance" if empty_source else "measured_net_mass",
                "representative": representative, "reason": reason, "physical_confirmation": True, "outputs": outputs})

    def reconcile(self, user_id, key, data):
        with self.db.write() as session:
            user, payload, previous = self._begin(session, user_id, key, "weight_adjustment", data)
            if previous:
                return previous
            state = _checked(session, data.get("container_id"), data.get("container_state"))
            reason = str(data.get("reason") or "").strip()
            if not reason:
                raise DomainError("Record why this container's mass is being adjusted")
            reading, basis, after = _weighing(state, data)
            if not state["mass_mg"] and not after:
                raise DomainError("This container is already recorded as empty; no adjustment is needed")
            op = self._operation(session, user, "weight_adjustment", max(state["mass_mg"], after))
            representative = data.get("representative") is True
            evidence = _evidence(self.db.environment, "gross less registered tare" if basis == "gross" else "net after tare")
            if basis == "gross":
                measure(session, op, "gross", reading, evidence, user, self.db.environment)
                evidence = {**evidence, "derived_from": "gross", "tare_mg": state["tare_mg"]}
            measure(session, op, "net", after, evidence, user, self.db.environment)
            portion = get(session, Portion, state["portion_id"]) if state["portion_id"] else None
            difference = after - state["mass_mg"]
            if portion:
                session.add(Input(operation_id=op.id, portion_id=portion.id, mass_mg=state["mass_mg"]))
                if difference:
                    post(session, op, portion, difference, "Container reweighing: " + reason[:150], user)
                if not after:
                    portion.status = "consumed"
                    assignment = get(session, Assignment, state["assignment_id"])
                    assignment.active, assignment.ended_at = False, now()
                else:
                    session.add(Output(operation_id=op.id, portion_id=portion.id, stream="adjusted", mass_mg=after))
                session.add(Profile(id=identity("profile"), operation_id=op.id, portion_id=portion.id, stream="adjusted", version=1,
                                    data=_profile(session, state, after, op, representative)))
            else:
                portion = produce(session, user, op, "found", after, state["container_id"], [], {
                    "schema": "beanosort-population-profile/v2", "composition_evidence": "unknown", "distributions": {},
                    "count_per_kg": None, "estimated_count": None, "material_population_unknown": True,
                    "note": "Previously unrecorded contents found by weighing; origin and composition are unknown."}, existing_mass_mg=after)
                portion.quality = "released"
            return self._finish(session, user, key, payload, op, {
                "container": state, "destination_id": state["container_id"], "before_mg": state["mass_mg"], "after_mg": after,
                "weight_adjustment_mg": difference, "weighing_basis": basis, "reading_mg": reading, "reason": reason,
                "representative": representative, "physical_confirmation": True,
                "above_nominal_mg": max(0, after - state["capacity_mg"]) if state["capacity_mg"] is not None else None,
                "outputs": [_result(portion if after else None, state["container_id"], after, "container reweighing")]})

    def package_product(self, user_id, key, data):
        with self.db.write() as session:
            user, payload, previous = self._begin(session, user_id, key, "product_packaging", data)
            if previous:
                return previous
            state = _checked(session, data.get("container_id"), data.get("container_state"), source=True)
            if not packaging_eligible(state):
                raise DomainError("Release the material's quality hold before packaging it as final product")
            basis = data.get("weighing_basis")
            if basis == "removed":
                reading = mass(data.get("weight"))
                if reading > state["mass_mg"]:
                    raise DomainError("The mass removed cannot exceed the recorded contents")
                after = state["mass_mg"] - reading
            else:
                reading, basis, after = _weighing(state, data)
            packaged = state["mass_mg"] - after
            if packaged <= 0:
                raise DomainError("The remaining net mass must be less than the recorded contents; use Reconciliation for a correction or increase")
            reason = str(data.get("reason") or "").strip()
            if len(reason) > 2000:
                raise DomainError("Packaging notes must be at most 2000 characters")
            op = self._operation(session, user, "product_packaging", state["mass_mg"])
            evidence = _evidence(self.db.environment, "removed contents net after tare" if basis == "removed" else
                                 "gross less registered tare" if basis == "gross" else "net after tare")
            if basis == "gross":
                measure(session, op, "remaining_gross", reading, evidence, user, self.db.environment)
                evidence = {**evidence, "derived_from": "remaining_gross", "tare_mg": state["tare_mg"]}
            if basis == "removed":
                measure(session, op, "packaged_net", reading, evidence, user, self.db.environment)
            else:
                measure(session, op, "remaining_net", after, evidence, user, self.db.environment)
            portion = get(session, Portion, state["portion_id"])
            session.add(Input(operation_id=op.id, portion_id=portion.id, mass_mg=state["mass_mg"]))
            post(session, op, portion, -packaged, "Final product: removed from inventory after packaging", user)
            representative = data.get("representative") is True
            if after:
                session.add(Output(operation_id=op.id, portion_id=portion.id, stream="remainder", mass_mg=after))
                session.add(Profile(id=identity("profile"), operation_id=op.id, portion_id=portion.id, stream="remainder", version=1,
                                    data=_profile(session, state, after, op, representative)))
            else:
                portion.status = "consumed"
                assignment = get(session, Assignment, state["assignment_id"])
                assignment.active, assignment.ended_at = False, now()
            return self._finish(session, user, key, payload, op, {
                "container": state, "destination_id": state["container_id"], "before_mg": state["mass_mg"], "after_mg": after,
                "packaged_mg": packaged, "disposition": "final_product",
                "mass_basis": "measured removed net mass" if basis == "removed" else "recorded contents less measured remaining net mass",
                "station_name": "Packaging desk", "weighing_basis": basis, "reading_mg": reading, "reason": reason,
                "representative": representative, "physical_confirmation": True,
                "outputs": [_result(portion if after else None, state["container_id"], after,
                    "calculated remainder; not separately weighed" if basis == "removed" else "measured contents remaining after packaging")]})
