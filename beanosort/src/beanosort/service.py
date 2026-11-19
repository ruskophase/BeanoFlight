"""Short transactional application commands. Never controls a machine."""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from .auth import require_role
from .capture import CAPTURE_DEFAULT, SimulatedMonitor, digest, expected_profile, fixture_particle, probability, simulated_destination, validate_particle
from .models import (Assignment, Audit, Command, Container, Destination, IngestBatch, Input, Ledger, Lineage,
                     Measurement, MonitorEvent, Operation, Output, Particle, Portion, Profile, Receipt,
                     Reconciliation, Reservation, Run, Station, User, Version, identity, now)
from .models import Location, SimulationAssumption, SimulationSettings, SourceBundle
from .quantities import DomainError, RECEIVING_CAPACITY_POLICY, kg, mass, receiving_capacity_limit_mg
from .simulation import MAX_SIMULATION_PARTICLES, current_settings, pin_assumption


def get(session, model, key):
    value = session.get(model, key)
    if value is None:
        raise DomainError(f"Unknown {model.__name__.lower()} identifier")
    return value


def audit(session, user, action, data, operation=None, station=None):
    session.add(Audit(id=identity("audit"), operator_id=user.id, action=action, data=data,
                      operation_id=operation.id if operation else None,
                      station_id=operation.station_id if operation else station))


def balance(session, portion_id):
    # PostgreSQL SUM(bigint) returns numeric/Decimal; authoritative values remain
    # integer milligrams at the application boundary (without a float conversion).
    return int(session.scalar(select(func.coalesce(func.sum(Ledger.delta_mg), 0)).where(Ledger.portion_id == portion_id)))


def active_assignment(session, **filters):
    return session.scalar(select(Assignment).filter_by(active=True, **filters))


def available(session, portion):
    if portion.status != "stock" or portion.quality != "released":
        return 0
    reserved = session.scalar(select(func.coalesce(func.sum(Reservation.mass_mg), 0)).where(
        Reservation.portion_id == portion.id, Reservation.active == True))
    return balance(session, portion.id) - int(reserved)


def empty_container(session, container_id, amount=None, *, receiving=False):
    container = get(session, Container, container_id)
    if container.deleted:
        raise DomainError("Container is deleted and cannot receive material")
    if container.archived:
        raise DomainError("Container is archived; restore it before assigning material")
    if active_assignment(session, container_id=container.id):
        raise DomainError("Container already has contents; a combination operation is required")
    if session.scalar(select(Destination.id).filter_by(container_id=container.id, active=True)):
        raise DomainError("Container is reserved by another job")
    limit = receiving_capacity_limit_mg(container.capacity_mg) if receiving else container.capacity_mg
    if amount is not None and limit is not None and amount > limit:
        if receiving:
            raise DomainError(f"Receiving mass exceeds 120% of nominal container capacity (maximum {kg(limit)} kg)")
        raise DomainError("Container capacity exceeded")
    return container


def receiving_capacity_snapshot(container, amount):
    return {"policy": RECEIVING_CAPACITY_POLICY, "nominal_capacity_mg": container.capacity_mg,
            "receiving_limit_mg": receiving_capacity_limit_mg(container.capacity_mg),
            "above_nominal_mg": max(0, amount - container.capacity_mg) if container.capacity_mg is not None else None}


def container_snapshot(container):
    return {field: getattr(container, field) for field in
            ("id", "label", "location", "location_id", "kind", "capacity_mg", "tare_mg", "archived", "deleted", "revision")}


def location_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise DomainError("Location name is required and must be at most 200 characters")
    return value.strip()


def resolve_location(session, user, *, location_id=None, name=None):
    if location_id:
        item = get(session, Location, location_id)
        if name and location_name(name).casefold() != item.name_key:
            raise DomainError("Location name does not match the selected location")
        return item
    # Compatibility for trusted CLI callers and forms opened before this upgrade.
    # New browser forms use IDs from the registered-location picker.
    name = location_name(name)
    item = session.scalar(select(Location).filter_by(name_key=name.casefold()))
    if item:
        return item
    item = Location(id=identity("location"), name=name, name_key=name.casefold())
    session.add(item)
    session.flush()
    audit(session, user, "location_created", {"location_id": item.id, "name": item.name})
    return item


def container_usage(session, container):
    assignment = active_assignment(session, container_id=container.id)
    destination = session.scalar(select(Destination.id).filter_by(container_id=container.id, active=True))
    reserved = assignment and session.scalar(select(Reservation.id).filter_by(portion_id=assignment.portion_id, active=True))
    wip = assignment and get(session, Portion, assignment.portion_id).status == "wip"
    used = bool(assignment or session.scalar(select(Assignment.id).filter_by(container_id=container.id).limit(1)) or
                session.scalar(select(Destination.id).filter_by(container_id=container.id).limit(1)))
    from .segments import movable_collection
    movable=not (destination or reserved or wip) or movable_collection(session,container.id)
    return {"occupied": bool(assignment), "history": used,"can_move":movable,
            "mass_mg": balance(session, assignment.portion_id) if assignment else 0,
            "locked": bool(destination or reserved or wip)}


def check_revision(container, value, label="Container"):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DomainError(f"Reload the {label.lower()} before changing it")
    try:
        revision = int(value)
    except ValueError:
        raise DomainError(f"Reload the {label.lower()} before changing it") from None
    if revision != container.revision:
        raise DomainError(f"{label} changed in another session; reload before saving")


def deleted_container(session, key):
    return session.scalar(select(Audit).where(Audit.action == "container_deleted", Audit.data["container_id"].as_string() == key))


def deleted_location(session, key):
    return session.scalar(select(Audit).where(Audit.action == "location_deleted", Audit.data["location_id"].as_string() == key))


def require_unstarted_sorting(session, operation):
    from .models import MachineJob
    machine = session.get(MachineJob, operation.id)
    if machine and machine.state != "prepared":
        raise DomainError("A machine start has already been requested; use its execution controls")
    if operation.kind != "sorting" or operation.status != "prepared" or session.scalar(select(Run.id).filter_by(operation_id=operation.id)):
        raise DomainError("Only a prepared sorting job that has never started can be edited or deleted")
    if session.scalar(select(Output.portion_id).filter_by(operation_id=operation.id)) or session.scalar(select(Ledger.id).filter_by(operation_id=operation.id)):
        raise DomainError("This job already has material postings and cannot be edited or deleted")


def prepared_snapshot(session, operation):
    from .collections import feed_input
    source = feed_input(session, operation)
    destinations = {d.stream: d.container_id for d in session.scalars(select(Destination).filter_by(operation_id=operation.id))}
    assumption = session.scalar(select(SimulationAssumption).filter_by(operation_id=operation.id))
    return {"operation_id": operation.id, "portion_id": source.portion_id if source else None,
            "planned_mg": operation.planned_mg, "boundary_mg": operation.boundary_mg,
            "station_id": operation.station_id, "recipe_id": operation.recipe_id,
            "capture_model_id": operation.capture_model_id, "policy_id": operation.policy_id,
            "selected_id": destinations.get("selected"), "pass_through_id": destinations.get("pass_through"),
            "simulation_settings_id": assumption.settings_id if assumption else None}


def release_prepared_claims(session, operation):
    for model in (Reservation, Destination):
        for claim in session.scalars(select(model).filter_by(operation_id=operation.id, active=True)):
            claim.active = False
    session.flush()


def measure(session, operation, role, amount, evidence, user, environment):
    kind = evidence.get("kind")
    allowed = {"simulation"} if environment == "simulation" else {"direct_measurement", "operator_declaration", "supplier_declaration"}
    if kind not in allowed:
        raise DomainError("Mass evidence does not match this inventory environment")
    if not evidence.get("method") or not evidence.get("tare_basis"):
        raise DomainError("Record measurement method and tare basis")
    if evidence.get("precision_mg") is not None:
        precision = mass(str(evidence["precision_mg"]), "mg")
        if amount % precision:
            raise DomainError("Mass is more precise than the declared measurement resolution")
    measured_at = evidence.get("measured_at") or now()
    session.add(Measurement(id=identity("measurement"), operation_id=operation.id, role=role, mass_mg=amount,
                            provenance={**evidence, "operator_id": user.id, "measured_at": measured_at,
                                        "recorded_at": now(), "unit": "mg"}))


def post(session, operation, portion, amount, reason, user):
    if balance(session, portion.id) + amount < 0:
        raise DomainError("Ledger posting would create negative stock")
    session.add(Ledger(id=identity("movement"), operation_id=operation.id, portion_id=portion.id,
                       delta_mg=amount, reason=reason, operator_id=user.id, station_id=operation.station_id))


def replay(session, key, payload):
    if not isinstance(key, str) or not 8 <= len(key) <= 100:
        raise DomainError("Provide a stable idempotency key (8–100 characters)")
    old = session.get(Command, key)
    if old and old.digest != digest(payload):
        raise DomainError("Idempotency key already used with different content")
    return old.result_id if old else None


def remember(session, key, payload, result_id):
    session.add(Command(key=key, digest=digest(payload), result_id=result_id))


class Workflow:
    def __init__(self, database):
        self.db = database

    def create_location(self, user_id, key, name):
        payload = {"command": "create_location", "operator": user_id, "name": name}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            name = location_name(name)
            if s.scalar(select(Location.id).filter_by(name_key=name.casefold())):
                raise DomainError("Location already exists; select the existing location")
            item = resolve_location(s, user, name=name)
            remember(s, key, payload, item.id)
            return item.id

    def create_container(self, user_id, *, label, location=None, location_id=None, kind="bin", capacity=None, tare_mass=None):
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            if not label.strip():
                raise DomainError("Container label and location are required")
            if capacity is None or (isinstance(capacity, str) and not capacity.strip()):
                raise DomainError("Container capacity is required (kg)")
            if tare_mass is None or (isinstance(tare_mass, str) and not tare_mass.strip()):
                raise DomainError("Container tare weight is required (kg)")
            capacity_mg, tare_mg = mass(capacity), mass(tare_mass)
            if s.scalar(select(Container.id).filter_by(label=label.strip())):
                raise DomainError("Container label already exists")
            place = resolve_location(s, user, location_id=location_id, name=location)
            c = Container(id=identity("bin"), label=label.strip(), location=place.name, location_id=place.id, kind=kind,
                          capacity_mg=capacity_mg, tare_mg=tare_mg)
            s.add(c)
            audit(s, user, "container_created", {"container_id": c.id, "label": c.label, "location": c.location, "location_id": c.location_id,
                "capacity_mg": capacity_mg, "capacity_basis": "nominal net material mass",
                "tare_mg": tare_mg, "tare_evidence": "simulation" if self.db.environment == "simulation" else "operator_declaration",
                "tare_method": "empty container mass entered at registration", "unit": "mg"})
            return c.id

    def update_location(self, user_id, key, location_id, *, name, revision):
        payload = {"command": "update_location", "operator": user_id, "location_id": location_id, "name": name, "revision": revision}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            place = get(s, Location, location_id)
            check_revision(place, revision, "Location")
            name = location_name(name)
            if s.scalar(select(Location.id).where(Location.name_key == name.casefold(), Location.id != place.id)):
                raise DomainError("Location name already exists")
            before = {"id": place.id, "name": place.name, "name_key": place.name_key, "revision": place.revision}
            ids = list(s.scalars(select(Container.id).filter_by(location_id=place.id)))
            place.name, place.name_key = name, name.casefold()
            place.revision += 1
            s.flush()  # Database trigger updates container display names/revisions.
            audit(s, user, "location_updated", {"location_id": place.id, "before": before,
                "after": {"id": place.id, "name": place.name, "name_key": place.name_key, "revision": place.revision},
                "container_ids": ids, "reason": "Storage location name updated"})
            remember(s, key, payload, place.id)
            return place.id

    def delete_location(self, user_id, key, location_id, *, revision, confirmed=False):
        payload = {"command": "delete_location", "operator": user_id, "location_id": location_id,
                   "revision": revision, "confirmed": confirmed}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            place = get(s, Location, location_id)
            check_revision(place, revision, "Location")
            if s.scalar(select(Container.id).filter_by(location_id=place.id).limit(1)):
                raise DomainError("Location still contains containers; move or remove them before deleting it")
            if confirmed is not True:
                raise DomainError("Confirm deletion of this empty location")
            audit(s, user, "location_deleted", {"location_id": place.id,
                "before": {"id": place.id, "name": place.name, "name_key": place.name_key, "revision": place.revision},
                "reason": "Empty storage location deleted"})
            s.delete(place)
            remember(s, key, payload, place.id)
            return place.id

    def update_container(self, user_id, key, container_id, data):
        payload = {"command": "update_container", "operator": user_id, "container_id": container_id, **data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            container = get(s, Container, container_id)
            if container.deleted:
                raise DomainError("Container has been deleted")
            check_revision(container, data.get("revision"))
            usage = container_usage(s, container)
            if container.archived:
                raise DomainError("Restore this archived container before editing it")
            if usage["locked"]:
                raise DomainError("Container is in an active job; finish or cancel that boundary before editing")
            label, kind = (str(data.get(f) or "").strip() for f in ("label", "kind"))
            if not 1 <= len(label) <= 120 or not 1 <= len(kind) <= 40:
                raise DomainError("Provide a name (up to 120 characters), location (200) and vessel type (40)")
            if s.scalar(select(Container.id).where(Container.label == label, Container.id != container.id)):
                raise DomainError("Container label already exists")
            for field, title in (("capacity", "capacity"), ("tare_mass", "tare weight")):
                if data.get(field) is None or (isinstance(data[field], str) and not data[field].strip()):
                    raise DomainError(f"Container {title} is required (kg)")
            capacity, tare = mass(data["capacity"]), mass(data["tare_mass"])
            if capacity < usage["mass_mg"] and (container.capacity_mg is None or capacity < container.capacity_mg):
                raise DomainError("Capacity cannot be reduced below the material currently in the container")
            place = resolve_location(s, user, location_id=data.get("location_id"), name=data.get("location"))
            before = container_snapshot(container)
            container.label, container.location, container.location_id, container.kind = label, place.name, place.id, kind
            container.capacity_mg, container.tare_mg = capacity, tare
            container.revision += 1
            changes = {"container_id": container.id, "before": before, "after": container_snapshot(container),
                       "reason": str(data.get("reason") or "Container details updated").strip()[:2000]}
            if tare != before["tare_mg"]:
                changes.update(tare_mg=tare, tare_evidence="simulation" if self.db.environment == "simulation" else "operator_declaration",
                               tare_method="empty container mass entered during container edit", unit="mg")
            audit(s, user, "container_updated", changes)
            remember(s, key, payload, container.id)
            return container.id

    def move_container(self, user_id, key, container_id, *, location_id, revision):
        payload = {"command": "move_container", "operator": user_id, "container_id": container_id,
                   "location_id": location_id, "revision": revision}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            container = get(s, Container, container_id)
            check_revision(container, revision)
            if container.deleted or container.archived or not container_usage(s, container)["can_move"]:
                raise DomainError("Restore the container or finish its active job before moving it")
            place = get(s, Location, location_id)
            if place.id != container.location_id:
                before = container_snapshot(container)
                container.location, container.location_id = place.name, place.id
                container.revision += 1
                audit(s, user, "container_moved", {"container_id": container.id, "before": before,
                    "after": container_snapshot(container), "reason": "Operator recorded container storage location",
                    "evidence": "simulation" if self.db.environment == "simulation" else "operator_declaration"})
            remember(s, key, payload, container.id)
            return container.id

    def retire_container(self, user_id, key, container_id, *, revision, action, confirmed=False, reason=""):
        payload = {"command": "retire_container", "operator": user_id, "container_id": container_id,
                   "revision": revision, "action": action, "confirmed": confirmed, "reason": reason}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            container = get(s, Container, container_id)
            if container.deleted:
                raise DomainError("Container has already been deleted")
            check_revision(container, revision)
            if action not in {"archive", "restore", "delete"}:
                raise DomainError("Unknown container action")
            usage = container_usage(s, container)
            if usage["occupied"] or usage["locked"]:
                raise DomainError("Container has contents or is in an active job; it cannot be removed")
            before = container_snapshot(container)
            if action == "delete":
                if confirmed is not True:
                    raise DomainError("Confirm deletion of this unused container")
                if usage["history"]:
                    container.deleted = True
                    container.location_id = None
                    container.revision += 1
                else:
                    s.delete(container)
            else:
                target = action == "archive"
                if container.archived == target:
                    raise DomainError("Container is already " + ("archived" if target else "active"))
                container.archived = target
                container.revision += 1
            audit(s, user, {"archive": "container_archived", "restore": "container_restored", "delete": "container_deleted"}[action],
                  {"container_id": container.id, "before": before,
                   "after": None if action == "delete" else container_snapshot(container),
                   "reason": (reason.strip() or f"Container {action}")[:2000]})
            remember(s, key, payload, container.id)
            return container.id

    def create_version(self, user_id, kind, name, data):
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "admin")
            if kind == "capture_model":
                probability(data)
            elif kind == "reconciliation_policy":
                if type(data.get("tolerance_mg")) is not int:
                    raise DomainError("Policy tolerance_mg must be an integer")
                mass(str(data["tolerance_mg"]), "mg", zero=True)
                if type(data.get("allow_estimated_composition")) is not bool:
                    raise DomainError("Set an explicit estimated-composition release policy")
            elif kind in {"product_specification", "packaging_specification"}:
                from .handling import validate_specification
                validate_specification(kind, data)
            elif kind == "recipe" and data.get("schema") == "beanosort-reference-recipe/v1":
                from .references import validate_recipe
                validate_recipe(s, data)
            elif kind != "recipe":
                raise DomainError("Only recipe, capture model and reconciliation policy versions are executable in Phase 1")
            number = (s.scalar(select(func.max(Version.version)).filter_by(kind=kind, name=name)) or 0) + 1
            item = Version(id=identity("version"), kind=kind, name=name, version=number, data=data, operator_id=user.id)
            s.add(item)
            audit(s, user, "version_created", {"version_id": item.id, "kind": kind, "version": number})
            return item.id

    def receive(self, user_id, key, data):
        payload = {"command": "receive", "operator": user_id, **data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            if data.get("ready") is not True:
                raise DomainError("Confirm the receiving container is physically empty, clean and ready")
            amount = mass(data["net_mass"], data.get("unit", "kg"))
            gross, tare = data.get("gross_mass"), data.get("tare_mass")
            if bool(gross) != bool(tare):
                raise DomainError("Provide both gross and tare, or neither")
            evidence = dict(data["evidence"])
            if gross is not None and tare is not None:
                gross_mg, tare_mg = mass(gross, data.get("unit", "kg")), mass(tare, data.get("unit", "kg"), zero=True)
                if gross_mg - tare_mg != amount:
                    raise DomainError("Gross minus tare must equal recorded net mass")
                evidence.update(gross_mg=gross_mg, tare_mg=tare_mg)
            c = empty_container(s, data["container_id"], amount, receiving=True)
            evidence["receiving_capacity"] = receiving_capacity_snapshot(c, amount)
            station = get(s, Station, data["station_id"])
            for field in ("purchase_date", "received_date"):
                if data.get(field):
                    try:
                        date.fromisoformat(data[field])
                    except ValueError:
                        raise DomainError("Dates must use YYYY-MM-DD") from None
            operation = Operation(id=identity("op"), kind="receiving", status="completed", release_status="released",
                                  execution_method="simulated" if self.db.environment == "simulation" else "manual",
                                  station_id=station.id, operator_id=user.id, boundary_mg=amount)
            s.add(operation)
            s.flush()
            if data.get("simulation_settings_id"):
                pin_assumption(s, operation, get(s, SimulationSettings, data["simulation_settings_id"]), amount)
            receipt = Receipt(id=identity("receipt"), source_lot_id=identity("lot"), operation_id=operation.id,
                              **{k: data.get(k) or None for k in ("supplier", "supplier_lot", "origin", "bean_type", "purchase_date")},
                              received_date=data.get("received_date") or now()[:10], notes=data.get("notes", ""),
                              declarations={"evidence": "supplier_declaration", "characteristics": data.get("declared_characteristics", {})})
            s.add(receipt)
            s.flush()
            portion = Portion(id=identity("portion"), receipt_id=receipt.id, created_by=operation.id, status="stock", quality="released")
            s.add(portion)
            s.flush()
            measure(s, operation, "input", amount, evidence, user, self.db.environment)
            post(s, operation, portion, amount, "received", user)
            s.add(Output(operation_id=operation.id, portion_id=portion.id, stream="received", mass_mg=amount))
            s.add(Assignment(id=identity("assignment"), container_id=c.id, portion_id=portion.id, operation_id=operation.id))
            audit(s, user, "material_received", {"receipt_id": receipt.id, "portion_id": portion.id,
                "container_id": c.id, "ready_confirmation": True, "mass_mg": amount,
                "receiving_capacity": evidence["receiving_capacity"],
                "quality_note": "accepted as input stock; product compliance unknown"}, operation)
            remember(s, key, payload, portion.id)
            return portion.id

    def receive_loads(self, user_id, key, source, loads, *, station_id, ready, placed):
        """Receive one source into independently weighed, empty labelled vessels.

        Supplier bags are metadata only. All quantities are entered net readings
        after zeroing each empty vessel; registered tare is never subtracted again.
        """
        payload = {"command": "receive_loads", "operator": user_id, "source": source, "loads": loads,
                   "station_id": station_id, "ready": ready, "placed": placed}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            if ready is not True:
                raise DomainError("Confirm every receiving container was empty, clean and ready before loading")
            if placed is not True:
                raise DomainError("Confirm the loaded containers are at the selected storage locations")
            if not isinstance(loads, list) or not 1 <= len(loads) <= 20 or any(not isinstance(row, dict) for row in loads):
                raise DomainError("Receive into 1–20 containers at a time")
            if any(not row.get("container_id") for row in loads) or len({row["container_id"] for row in loads}) != len(loads):
                raise DomainError("Choose a different empty container for each load")
            station = get(s, Station, station_id)
            if station.kind != "manual":
                raise DomainError("Choose a receiving/weighing station")
            for field in ("purchase_date", "received_date"):
                if source.get(field):
                    try:
                        date.fromisoformat(source[field])
                    except (ValueError, TypeError):
                        raise DomainError("Dates must use YYYY-MM-DD") from None
            prepared = []
            for row in loads:
                amount = mass(row.get("net_mass"), "kg")
                if amount % 1000:
                    raise DomainError("Enter net bean mass in kg to at most three decimal places")
                container = empty_container(s, row["container_id"])
                if row.get("revision") not in (None, ""):
                    check_revision(container, row["revision"])
                before = container_snapshot(container)
                weights = {}
                for field, attribute, title in (("tare_mass", "tare_mg", "empty-container tare"), ("capacity", "capacity_mg", "nominal capacity")):
                    registered = getattr(container, attribute)
                    entered = row.get(field)
                    if entered in (None, ""):
                        if registered is None:
                            raise DomainError(f"Enter the {title} (kg) for {container.label} before receiving")
                        weights[attribute] = registered
                    else:
                        weights[attribute] = mass(entered)
                        if registered is not None and weights[attribute] != registered:
                            raise DomainError(f"The {title} is already registered; use Edit container to change it")
                if weights["tare_mg"] != container.tare_mg or weights["capacity_mg"] != container.capacity_mg:
                    container.tare_mg, container.capacity_mg = weights["tare_mg"], weights["capacity_mg"]
                    container.revision += 1
                    changes = {"container_id": container.id, "before": before, "after": container_snapshot(container),
                        "reason": "Missing container weights completed during receiving"}
                    if before["tare_mg"] is None:
                        changes.update(tare_mg=container.tare_mg,
                            tare_evidence="simulation" if self.db.environment == "simulation" else "operator_declaration",
                            tare_method="empty container tare entered during receiving", unit="mg")
                    audit(s, user, "container_updated", changes, station=station.id)
                empty_container(s, container.id, amount, receiving=True)
                if row.get("tared") is not True:
                    raise DomainError("Confirm the empty container was zeroed on the scale before its beans were weighed")
                place = get(s, Location, row.get("location_id"))
                prepared.append((container, amount, place))
            total = sum(amount for _, amount, _ in prepared)
            operation = Operation(id=identity("op"), kind="receiving", status="completed", release_status="released",
                execution_method="simulated" if self.db.environment == "simulation" else "manual",
                station_id=station.id, operator_id=user.id, boundary_mg=total)
            s.add(operation)
            s.flush()
            receipt = Receipt(id=identity("receipt"), source_lot_id=identity("lot"), operation_id=operation.id,
                **{field: source.get(field) or None for field in ("supplier", "supplier_lot", "origin", "bean_type", "purchase_date")},
                received_date=source.get("received_date") or now()[:10], notes=source.get("notes") or "",
                declarations={"evidence": "supplier_declaration", "characteristics": {}})
            s.add(receipt)
            s.flush()
            records = []
            for index, (container, amount, place) in enumerate(prepared, 1):
                before = container_snapshot(container)
                provenance = {"kind": "simulation" if self.db.environment == "simulation" else "direct_measurement",
                    "method": "operator-entered scale net reading after zeroing the empty container",
                    "tare_basis": "empty receiving container zeroed on scale; entered mass is beans only",
                    "container_id": container.id, "container_revision": container.revision,
                    "registered_tare_mg": container.tare_mg, "scale_zeroed_confirmation": True,
                    "receiving_capacity": receiving_capacity_snapshot(container, amount),
                    "entry_unit": "kg", "entry_decimal_places": 3,
                    "storage_location_id": place.id, "storage_location": place.name}
                portion = Portion(id=identity("portion"), receipt_id=receipt.id, created_by=operation.id,
                                  status="stock", quality="released")
                s.add(portion)
                s.flush()
                measure(s, operation, f"input_{index}", amount, provenance, user, self.db.environment)
                post(s, operation, portion, amount, "received into empty labelled container", user)
                s.add(Output(operation_id=operation.id, portion_id=portion.id, stream=f"received_{index}", mass_mg=amount))
                if container.location_id != place.id:
                    container.location_id, container.location = place.id, place.name
                    container.revision += 1
                    audit(s, user, "container_moved", {"container_id": container.id, "before": before,
                        "after": container_snapshot(container), "reason": "Loaded container placed in receiving storage location",
                        "evidence": "simulation" if self.db.environment == "simulation" else "operator_declaration"}, operation)
                s.add(Assignment(id=identity("assignment"), container_id=container.id, portion_id=portion.id, operation_id=operation.id))
                records.append({"container_id": container.id, "portion_id": portion.id, "mass_mg": amount,
                                "receiving_capacity": provenance["receiving_capacity"],
                                "registered_tare_mg": provenance["registered_tare_mg"], "location_id": place.id})
            audit(s, user, "material_received", {"receipt_id": receipt.id, "source_lot_id": receipt.source_lot_id,
                "loads": records, "mass_mg": total, "ready_confirmation": True, "storage_confirmation": True,
                "properties": "unknown apart from entered mass and source declarations",
                "quality_note": "accepted as input stock; product compliance unknown"}, operation)
            remember(s, key, payload, receipt.id)
            return receipt.id

    def prepare(self, user_id, key, data):
        payload = {"command": "prepare", "operator": user_id, **data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            operation_id = self._prepare_sorting(s, user, data)
            remember(s, key, payload, operation_id)
            return operation_id

    def _prepare_sorting(self, s, user, data):
        from .sorting_modes import is_machine_recipe
        machine = is_machine_recipe(get(s, Version, data["recipe_id"]).data)
        if machine and not data.get("machine"):
            raise DomainError("Choose machine settings, recording mode and duration for All Pass-through")
        if data.get("machine") and not machine:
            raise DomainError("Machine execution requires the All Pass-through recipe")
        source = get(s, Portion, data["portion_id"])
        from .models import ProductLot
        if s.scalar(select(ProductLot.id).filter_by(portion_id=source.id)):
            raise DomainError("Opened-pack rework requires a separate operation; packaged material cannot be sorted as bulk")
        assignment = active_assignment(s, portion_id=source.id)
        amount = balance(s, source.id)
        if not assignment or available(s, source) != amount or amount <= 0:
            raise DomainError("Source must be released, available stock in a container")
        planned = mass(data["planned_mass"])
        if planned > amount:
            raise DomainError("Intended processing mass exceeds source stock")
        if data.get("ready") is not True:
            raise DomainError("Confirm both destinations and any existing contents are ready to receive beans")
        selected, passed = data.get("selected_id") or None, data.get("pass_through_id")
        from .sorting_modes import is_hopper_recipe
        hopper = is_hopper_recipe(get(s, Version, data["recipe_id"]).data)
        if hopper and selected:
            raise DomainError("Hopper emptying has no Selected container")
        if not hopper and len({assignment.container_id, selected, passed}) != 3:
            raise DomainError("Source and two destinations must be different containers")
        if not passed or (not hopper and not selected):
            raise DomainError("Choose the required destination containers")
        simulation = None
        capture_id = data.get("capture_model_id")
        if self.db.environment == "simulation" and (data.get("simulation_settings_id") or not capture_id):
            simulation = get(s, SimulationSettings, data["simulation_settings_id"]) if data.get("simulation_settings_id") else current_settings(s)
            if simulation is None:
                raise DomainError("Initialize Simulation Settings before preparing this job")
            if capture_id and capture_id != simulation.capture_model_id:
                raise DomainError("Capture model must match the accepted Simulation Settings version")
            capture_id = simulation.capture_model_id
        for version_id, kind in ((data["recipe_id"], "recipe"), (capture_id, "capture_model"), (data["policy_id"], "reconciliation_policy")):
            if get(s, Version, version_id).kind != kind:
                raise DomainError(f"Invalid {kind} version")
        recipe = get(s, Version, data["recipe_id"])
        from .references import retired_recipe_ids
        if recipe.id in retired_recipe_ids(s):
            raise DomainError("This recipe is retired; choose a current sorting recipe")
        source_population = None
        dataset = None
        if recipe.data.get("schema") == "beanosort-reference-recipe/v1":
            from .references import validate_recipe
            dataset = validate_recipe(s, recipe.data)
            if self.db.environment != "simulation":
                raise DomainError("Reference-data recipes currently execute only in isolated simulation inventory")
            from .population_sources import source_snapshot
            source_population = source_snapshot(s, source, dataset, simulation or current_settings(s), planned)
        station = get(s, Station, data["station_id"])
        method = "simulated" if self.db.environment == "simulation" else "manual"
        if method not in station.capabilities.get("execution_methods", []):
            raise DomainError("Station does not support this execution method")
        operation = Operation(id=identity("op"), kind="sorting", status="prepared", execution_method=method,
            station_id=station.id, operator_id=user.id, planned_mg=planned, boundary_mg=amount,
            recipe_id=data["recipe_id"], capture_model_id=capture_id, policy_id=data["policy_id"])
        s.add(operation)
        s.flush()
        if source_population:
            from .population_sources import INPUT_STREAM
            s.add(Profile(id=identity("profile"), operation_id=operation.id, stream=INPUT_STREAM, version=1, data=source_population))
        if simulation and not machine:
            pin_assumption(s, operation, simulation, planned, source_population=source_population)
        if machine:
            from .models import MachineJob
            spec = data["machine"]
            s.add(MachineJob(operation_id=operation.id, remote_id=spec["remote_id"], endpoint=spec["endpoint"],
                             request={**spec["request"],"start_paused":True}, state="prepared", cursor=0, status={}))
        s.add(Input(operation_id=operation.id, portion_id=source.id, mass_mg=amount))
        s.add(Reservation(id=identity("reservation"), operation_id=operation.id, portion_id=source.id, mass_mg=amount))
        for stream, container_id in (("selected", selected), ("pass_through", passed)):
            if not container_id:
                continue
            from .collections import pin_destination
            pin_destination(s, user, operation, container_id, stream,
                            dataset=dataset if self.db.environment == "simulation" else None, settings=simulation or current_settings(s),
                            source_return=hopper and container_id == assignment.container_id)
        audit(s, user, "job_prepared", {"boundary_mg": amount, "planned_mg": planned,
            "source_container_id": assignment.container_id, "requested_destinations": {"selected": selected, "pass_through": passed}}, operation)
        return operation.id

    def edit_prepared_sorting(self, user_id, key, operation_id, data, *, confirm_unstarted=False):
        payload = {"command": "edit_prepared_sorting", "operator": user_id, "operation_id": operation_id,
                   "data": data, "confirm_unstarted": confirm_unstarted}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            operation = get(s, Operation, operation_id)
            require_unstarted_sorting(s, operation)
            if confirm_unstarted is not True:
                raise DomainError("Confirm this job has not run and no material has been processed")
            before = prepared_snapshot(s, operation)
            release_prepared_claims(s, operation)
            # Validate/reserve the replacement inside this SAME transaction. Any
            # failure restores the original claims and prepared state on rollback.
            replacement_id = self._prepare_sorting(s, user, data)
            replacement = get(s, Operation, replacement_id)
            operation.status = "cancelled"
            audit(s, user, "job_replaced", {"replacement_id": replacement_id, "before": before,
                "after": prepared_snapshot(s, replacement), "confirmed_unstarted": True,
                "reason": "Prepared job corrected before execution"}, operation)
            audit(s, user, "job_replacement_created", {"replaces_id": operation.id}, replacement)
            remember(s, key, payload, replacement_id)
            return replacement_id

    def delete_prepared_sorting(self, user_id, key, operation_id, *, confirm_unstarted=False):
        payload = {"command": "delete_prepared_sorting", "operator": user_id, "operation_id": operation_id,
                   "confirm_unstarted": confirm_unstarted}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            operation = get(s, Operation, operation_id)
            require_unstarted_sorting(s, operation)
            if confirm_unstarted is not True:
                raise DomainError("Confirm this job has not run and no material has been processed")
            before = prepared_snapshot(s, operation)
            release_prepared_claims(s, operation)
            operation.status = "cancelled"
            from .models import MachineJob
            machine = s.get(MachineJob, operation.id)
            if machine: machine.state = "cancelled"
            audit(s, user, "job_deleted", {"before": before, "confirmed_unstarted": True,
                "reason": "Prepared sorting job deleted before execution; all reservations released"}, operation)
            remember(s, key, payload, operation.id)
            return operation.id

    def start(self, user_id, operation_id, *, seed="42", count=None, source_run_id=None):
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            op = get(s, Operation, operation_id)
            from .models import MachineJob
            machine = s.get(MachineJob, op.id)
            synthetic = self.db.environment == "simulation" and not machine
            if machine:
                source_run_id = machine.remote_id
            assumption = s.scalar(select(SimulationAssumption).filter_by(operation_id=op.id))
            count_basis = assumption.data if assumption else None
            from .population_sources import input_profile
            population_input = input_profile(s, op.id)
            if population_input and not count_basis:
                count_basis = population_input.data.get("count_basis")
            if count is None:
                count = count_basis["rounded_particles"] if count_basis else 1000
            existing = s.scalar(select(Run).filter_by(operation_id=op.id))
            if existing:
                if existing.seed != (str(seed) if synthetic else None) or existing.expected_particles != (count if synthetic else None):
                    raise DomainError("Run configuration is already fixed")
                if self.db.environment == "real" and source_run_id != existing.source_run_id:
                    raise DomainError("External source run identity is already fixed")
                return existing.id
            if op.status != "prepared":
                raise DomainError("Only prepared jobs can start")
            if synthetic and (type(count) is not int or not 1 <= count <= MAX_SIMULATION_PARTICLES or not str(seed) or len(str(seed)) > 200):
                raise DomainError("Simulation requires a seed and 1–1000000 particles. Adjust the planned mass or enter a smaller test count; estimates are never silently capped.")
            if not synthetic and not source_run_id:
                raise DomainError("Record an external local-run reference; this action only records workflow state")
            reference = None
            recipe = get(s, Version, op.recipe_id)
            if recipe.data.get("schema") == "beanosort-reference-recipe/v1":
                from .references import SAMPLER, load_dataset, validate_recipe
                dataset = validate_recipe(s, recipe.data)
                load_dataset(dataset, verify=True)
                reference = {"dataset_id": dataset.id, "fingerprint": dataset.fingerprint,
                             "bean_count": dataset.metadata_json["bean_count"], "method": SAMPLER,
                             "features_origin": "archived image-derived measurements",
                             "membership_evidence": "simulation; resampling with replacement",
                             "statistics_and_thresholds": recipe.data}
                if population_input:
                    reference.update(method=population_input.data["method"],
                                     input_population_profile_id=population_input.id,
                                     source_portion_id=population_input.data["source_portion_id"],
                                     source_population_fingerprint=population_input.data["fingerprint"],
                                     reference_bean_count=population_input.data["reference_bean_count"],
                                     membership_evidence="simulation; resampling from the retained source population")
            op.status = "running"
            from .collections import check_destination_contents
            check_destination_contents(s, op, start=True)
            for link in s.scalars(select(Input).filter_by(operation_id=op.id)):
                portion = get(s, Portion, link.portion_id)
                if portion.quality != "released" or portion.hold_reason:
                    raise DomainError("Source has been held since preparation")
                portion.status = "wip"
            run_id = identity("run")
            run = Run(id=run_id, operation_id=op.id, station_id=op.station_id,
                      source_run_id=run_id if synthetic else source_run_id,
                      seed=str(seed) if synthetic else None, expected_particles=count if synthetic else None,
                      source_versions={"machine_job": machine.remote_id if machine else None,
                                       "machine_manifest": machine.request if machine else None,
                                       "upstream": "machine-journal/v1" if machine else "reference-beans/v1" if reference else "synthetic-fixture/v1" if synthetic else "external-unverified",
                                       "reference_population": reference,
                                       "calibration": (machine.request.get("configuration") or {}).get("id", "unknown") if machine else "unknown",
                                       "illumination": machine.request["settings"].get("cameras", {}) if machine else "unknown",
                                       "recipe_id": op.recipe_id,
                                       "particle_count_estimate": count_basis if synthetic else None,
                                       "fixture_count_basis": ("mass / assumed mean, rounded half up" if count_basis and count == count_basis["rounded_particles"] else "operator-entered test count; physical coverage unknown") if synthetic else None,
                                       "decision_action_assumption": "every synthetic selection issues its intended action" if synthetic else None})
            s.add(run)
            audit(s, user, "workflow_started", {"run_id": run.id, "method": op.execution_method,
                  "hardware_action": "none"}, op)
            s.flush()
            from .run_summaries import ensure
            ensure(s, run, self.db.environment)
            from .sort_capacity import enforce
            enforce(s, user, op, self.db.environment)
            return run.id

    def transition(self, user_id, operation_id, target, *, reason="", drained=False):
        allowed = {"prepared": {"cancelled"}, "running": {"paused", "failed", "finishing"},
                   "paused": {"running", "failed", "finishing"}, "failed": {"finishing"},
                   "finishing": {"awaiting_reconciliation"}}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            op = get(s, Operation, operation_id)
            if op.status == target:
                return
            if target not in allowed.get(op.status, set()):
                raise DomainError("Invalid job transition")
            if target in {"failed", "paused", "cancelled"} and not reason.strip():
                raise DomainError("Record why the job was paused, failed or cancelled")
            if target == "awaiting_reconciliation" and not drained:
                raise DomainError("Confirm in-flight material has been accounted for")
            if target == "running":
                from .collections import check_destination_contents
                check_destination_contents(s, op, start=True)
                for link in s.scalars(select(Input).filter_by(operation_id=op.id)):
                    if get(s, Portion, link.portion_id).hold_reason:
                        raise DomainError("Source has an explicit quality hold; cannot resume processing")
                from .sort_capacity import acknowledge
                acknowledge(s, user, op, self.db.environment)
            previous = op.status
            op.status = target
            if target == "cancelled":
                for model in (Reservation, Destination):
                    for item in s.scalars(select(model).filter_by(operation_id=op.id, active=True)):
                        item.active = False
            audit(s, user, "execution_transition", {"from": previous, "to": target, "reason": reason,
                  "in_flight_accounted_for": drained}, op)

    def ingest(self, user_id, run_id, batch_key, rows, source_at=None, *, generation_segment=None, check_generation_segment=False):
        """Commit compact immutable logical particles, once per bounded batch.

        Source sequences are adapter-local zero-based particle ordinals, not
        BeanRegistry journal sequences. The future adapter must retain its own
        durable journal cursor. No late mutation of released profiles is allowed.
        """
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise DomainError("Use batches of 1–1000 particles")
        if not isinstance(batch_key, str) or not 1 <= len(batch_key) <= 200:
            raise DomainError("Batch identity must contain 1–200 characters")
        batch_digest = digest({"rows": rows, "source_at": source_at})
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            run = get(s, Run, run_id)
            op = get(s, Operation, run.operation_id)
            old = s.scalar(select(IngestBatch).filter_by(run_id=run.id, batch_key=batch_key))
            if old:
                if old.digest != batch_digest:
                    raise DomainError("Batch key reused with different data")
                return 0
            if check_generation_segment:
                if op.status != "running":
                    raise DomainError("Resume the simulation before generating more particles")
                from .segments import current
                segment=current(s,op)
                if (segment.id if segment else None) != generation_segment:
                    raise DomainError("Container assignments changed while this synthetic batch was being generated; retry after resuming")
            if op.status not in {"running", "paused", "failed", "finishing"}:
                raise DomainError("New/late evidence cannot alter a reconciled or closed run")
            model = get(s, Version, op.capture_model_id).data
            synthetic = self.db.environment == "simulation" and not run.source_versions.get("machine_job")
            reference = run.source_versions.get("reference_population")
            if reference:
                from .references import load_dataset, validate_recipe
                from .population_sources import sampler_for_run
                recipe = get(s, Version, op.recipe_id).data
                dataset = validate_recipe(s, recipe)
                source_rows, _ = load_dataset(dataset)
                sample = sampler_for_run(s, run, dataset, recipe, source_rows)
                if any(row != sample(row.get("sequence")) for row in rows):
                    raise DomainError("Reference particle differs from the pinned deterministic sample and recipe")
            for row in rows:
                validate_particle(row, "real" if run.source_versions.get("machine_job") else self.db.environment)
            from .run_summaries import ensure, add_particles
            ensure(s, run, self.db.environment)
            previous_by_key = {p.source_key: p for p in s.scalars(select(Particle).where(
                Particle.run_id == run.id, Particle.source_key.in_([r["key"] for r in rows])))}
            used_sequences = set(s.scalars(select(Particle.source_sequence).where(
                Particle.run_id == run.id, Particle.source_sequence.in_([r["sequence"] for r in rows]))))
            particles, events = [], []
            for row in rows:
                if run.expected_particles is not None and row["sequence"] >= run.expected_particles:
                    raise DomainError("Particle outside declared run range")
                row_digest = digest(row)
                previous = previous_by_key.get(row["key"])
                if previous:
                    if previous.digest != row_digest:
                        raise DomainError("Particle identity reused with different data")
                    continue
                if row["sequence"] in used_sequences:
                    raise DomainError("Source sequence reused for another particle")
                targeted = row["decision"] == "selected" and row["action"] == "attempted"
                destination = simulated_destination(run.id, row["key"], run.seed, targeted, model) if synthetic and (targeted or row["decision"] == "pass") else None
                particle = Particle(id=identity("particle"), run_id=run.id, source_key=row["key"], source_sequence=row["sequence"],
                    source_at=row.get("source_at"), payload=row, digest=row_digest, simulated_destination=destination)
                particles.append(particle)
                previous_by_key[row["key"]] = particle
                used_sequences.add(row["sequence"])
                if targeted and synthetic:
                    for event in SimulatedMonitor().events(destination, model):
                        events.append(MonitorEvent(id=identity("event"), run_id=run.id, particle_id=particle.id,
                                                   kind=event.kind, evidence=event.evidence, data=event.data))
            # Parent rows precede their monitor events, in one bounded transaction.
            s.add_all(particles)
            s.flush()
            s.add_all(events)
            inserted = len(particles)
            s.add(IngestBatch(id=identity("batch"), run_id=run.id, batch_key=batch_key, digest=batch_digest, source_at=source_at))
            get(s, Station, run.station_id).last_received_at = now()
            audit(s, user, "batch_ingested", {"run_id": run.id, "batch_key": batch_key, "new_particles": inserted}, op)
            s.flush()
            add_particles(s, run, particles, self.db.environment)
            from .sort_capacity import enforce
            enforce(s, user, op, self.db.environment)
            return inserted

    def simulate_next(self, user_id, run_id, *, replay_first=False):
        if self.db.environment != "simulation":
            raise DomainError("Synthetic upstream particles are disabled for real inventory")
        with self.db.read() as s:
            run = get(s, Run, run_id)
            if run.source_versions.get("machine_job"):
                raise DomainError("Machine jobs receive image-derived evidence; synthetic generation is disabled")
            op = get(s, Operation, run.operation_id)
            from .segments import current
            segment=current(s,op); generation_segment=segment.id if segment else None
            if not replay_first and op.status != "running":
                raise DomainError("Resume the simulation before generating more particles")
            # The usual contiguous case needs no growing per-particle Python set.
            from .run_summaries import get_summary
            summary = get_summary(s, run.id)
            total, last = ((summary.particle_count, summary.last_sequence) if summary else
                s.execute(select(func.count(Particle.id), func.max(Particle.source_sequence)).filter_by(run_id=run.id)).one())
            if replay_first:
                from .sort_capacity import first_batch_range
                start, end = first_batch_range(s, run)
            elif last is None or last + 1 == total:
                start = total
            else:
                sequences = set(s.scalars(select(Particle.source_sequence).filter_by(run_id=run.id)))
                start = next((i for i in range(run.expected_particles) if i not in sequences), run.expected_particles)
            if start == run.expected_particles:
                return 0
            # Partial batches stop at capacity thresholds; resume at the first
            # uncommitted particle. Replays retain their original saved range.
            if not replay_first:
                end = min((start // 250 + 1) * 250, run.expected_particles)
            if run.source_versions.get("reference_population"):
                from .references import load_dataset, validate_recipe
                from .population_sources import sampler_for_run
                recipe = get(s, Version, op.recipe_id).data
                dataset = validate_recipe(s, recipe)
                source_rows, _ = load_dataset(dataset)
                sample = sampler_for_run(s, run, dataset, recipe, source_rows)
                rows = [sample(i) for i in range(start, end)]
            else:
                rows = [fixture_particle(i) for i in range(start, end)]
            if not replay_first:
                from .sort_capacity import generation_prefix
                rows = generation_prefix(s, op, run, rows)
                end = start + len(rows)
        return self.ingest(user_id, run_id, f"{'reference-v1' if run.source_versions.get('reference_population') else 'fixture-v1'}:{start}:{end}", rows,
                           generation_segment=generation_segment,check_generation_segment=True)

    def profile(self, session, operation, *, collection=None):
        run = session.scalar(select(Run).filter_by(operation_id=operation.id))
        query=select(Particle).filter_by(run_id=run.id) if run else None
        if collection:
            from .segments import particle_filter
            condition=particle_filter(session,collection)
            if condition is None:
                return {"schema":"beanosort-population-profile/v2","composition_evidence":"unknown","coverage":"collection association unknown",
                        "count":None,"moisture":{"value":None,"evidence":"unknown"},"collection_id":collection.id,
                        "note":"No reliable local particle boundary was recorded; bin membership and composition are unknown."}
            query=query.where(condition) if query is not None else None
        from copy import deepcopy
        from .run_summaries import get_summary
        summary = get_summary(session, run.id, collection) if run else None
        particles = [] if summary else list(session.scalars(query)) if query is not None else []
        sequences = {p.source_sequence for p in particles}
        count = summary.particle_count if summary else len(sequences)
        last_sequence = summary.last_sequence if summary else max(sequences) if sequences else None
        if run and run.expected_particles is not None:
            missing = run.expected_particles - count
            coverage = "complete synthetic fixture" if missing == 0 else "incomplete synthetic fixture"
        else:
            missing = None
            coverage = "unknown"
        data = deepcopy(summary.data['profile']) if summary else expected_profile(
            [{"payload": p.payload, "simulated_destination": p.simulated_destination} for p in particles],
            get(session, Version, operation.capture_model_id).data,
            synthetic=self.db.environment == "simulation" and not (run and run.source_versions.get("machine_job")), coverage=coverage)
        data['coverage'] = coverage
        data.update(missing_source_particles=missing, run_id=run.id if run else None,
                    source_versions=run.source_versions if run else {}, recipe_id=operation.recipe_id,
                    capture_model_id=operation.capture_model_id,
                    source_sequence_gaps=last_sequence+1-count if last_sequence is not None else 0)
        if collection:
            data.update(collection_id=collection.id,collection_code=collection.code,logical_stream=collection.stream,
                        missing_source_particles=None,coverage="collection-window observations; physical coverage unknown",source_sequence_gaps=None)
        if run and run.source_versions.get("reference_population"):
            from .references import population_report, validate_recipe
            recipe = get(session, Version, operation.recipe_id).data
            dataset = validate_recipe(session, recipe)
            data["reference_population"] = deepcopy(summary.data['reference']) if summary else population_report(
                [{"payload": p.payload, "simulated_destination": p.simulated_destination} for p in particles],
                dataset, recipe, get(session, Version, operation.capture_model_id).data)
            data["operation_id"] = operation.id
            data["coverage"] = "complete reference-resampled simulation; physical source coverage unknown" if missing == 0 else "incomplete reference-resampled simulation"
        bundle = session.scalar(select(SourceBundle).filter_by(run_id=run.id)) if run else None
        if bundle:
            data["source_bundle"] = bundle.manifest
            data["source_versions"] = bundle.manifest["source_versions"]
            data["missing_source_particles"] = bundle.manifest["particle_count"] - count
            data["coverage"] = "archived tracked records imported; physical coverage unknown" if data["missing_source_particles"] == 0 else "incomplete archived import; physical coverage unknown"
            # Matching artifact provenance alone does not establish calibration,
            # illumination or cross-machine comparability. Keep it unknown.
            data["comparability_key"] = None
        if collection:
            data.update(coverage="collection-window observations; physical coverage unknown",missing_source_particles=None,
                        source_sequence_gaps=None)
        return data

    def reconcile(self, user_id, key, operation_id, data):
        payload = {"command": "reconcile", "operator": user_id, "operation_id": operation_id, **data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "operator", "supervisor", "admin")
            old = replay(s, key, payload)
            if old:
                return old
            op = get(s, Operation, operation_id)
            if op.status != "awaiting_reconciliation" or s.scalar(select(Reconciliation.id).filter_by(operation_id=op.id)):
                raise DomainError("Job must await reconciliation and may be reconciled only once")
            from .segments import current
            if current(s,op):
                from .segmented_reconciliation import reconcile
                return reconcile(self,s,user,op,key,payload,data)
            fields = ("selected", "pass_through", "remainder", "retained", "loss", "sample")
            if any(data.get(k) in (None, "") for k in fields):
                raise DomainError("Not reconciled: enter every mass, including explicit zeros")
            quantities = {k: mass(data[k], zero=True) for k in fields}
            from .sorting_modes import hopper_mode, source_return
            hopper, returning = hopper_mode(s, op), source_return(s, op)
            if hopper and quantities["selected"]:
                raise DomainError("Hopper emptying cannot create Selected material")
            if returning and quantities["remainder"]:
                raise DomainError("The shared Source / Pass-through weight already includes remaining beans")
            from .collections import additions, baselines, check_destination_contents, combined_profile, consume_prior, feed_input, starting_mass
            prior = baselines(s, op)
            added = additions(s, op, quantities)
            input_total = op.boundary_mg + starting_mass(s, op)
            discrepancy = input_total - sum(quantities.values())
            if not data.get("reason", "").strip():
                raise DomainError("Record weighing/accounting notes, including removals and unexplained differences")
            parent = get(s, Portion, feed_input(s, op).portion_id)
            check_destination_contents(s, op)
            original = active_assignment(s, portion_id=parent.id)
            if parent.status != "wip" or not original:
                raise DomainError("Source must remain assigned and in work-in-progress")
            weighing = data.get("container_weighing")
            if weighing:
                from .weighing import validate_and_record_tares
                if quantities["sample"] != 0:
                    raise DomainError("This container-weighing workflow does not remove samples")
                if weighing.get("discrepancy_mg") != discrepancy:
                    raise DomainError("The signed discrepancy changed; review the weights again")
                if weighing["model"].get("available") and self.db.environment != "simulation":
                    raise DomainError("Area-to-mass simulation predictions cannot become real measurements")
                validate_and_record_tares(s, user, op, original, weighing, quantities, self.db.environment)
            policy = get(s, Version, op.policy_id)
            model_profile = self.profile(s, op)
            for role, quantity in quantities.items():
                evidence = dict(data["evidence"])
                if weighing and role in weighing["containers"]:
                    entry = weighing["containers"][role]
                    evidence.update(container_id=entry["container_id"], container_revision=entry["effective_tare_revision"],
                        gross_mg=entry["gross_mg"], tare_mg=entry["tare_mg"], weighed_confirmation=True,
                        starting_mass_mg=entry.get("starting_mass_mg",0),starting_portion_id=entry.get("starting_portion_id"),
                        net_evidence="calculated_difference: gross minus tare", prediction_kept_separate=True)
                elif weighing:
                    evidence.update(kind="simulation" if self.db.environment == "simulation" else "operator_declaration",
                                    method="operator-declared removal/retention; sampling not used")
                    if hopper and role == "selected":
                        evidence["method"] = "Selected stream is unused by the pinned hopper-emptying recipe; no Selected container"
                    elif returning and role == "remainder":
                        evidence["method"] = "Remaining source beans are included in the one shared Source / Pass-through weighing"
                measure(s, op, role, quantity, evidence, user, self.db.environment)
            recon = Reconciliation(id=identity("reconciliation"), operation_id=op.id, discrepancy_mg=discrepancy,
                accounting={"input_mg": input_total, "feed_input_mg":op.boundary_mg,
                            "destination_starting_mg":{key:value.get("existing_mass_mg",0) for key,value in prior.items()},
                            "unchanged_destination_mg":{key:value.get("existing_mass_mg",0) for key,value in prior.items() if added.get(key)==0},
                            "added_mg":{key:None if returning and key == "pass_through" else value for key,value in added.items()},
                            **({"source_return":{"single_final_total":True,"hopper_return_mass_mg":None,
                                "separate_unprocessed_remainder_known":False}} if returning else {}),
                            **{f"{k}_mg": v for k, v in quantities.items()},
                            "unexplained_difference_mg": discrepancy,
                            **({"container_weighing": weighing} if weighing else {})}, reason=data["reason"], policy_id=policy.id)
            s.add(recon)
            parent.status = "consumed"
            original.active = False
            original.ended_at = now()
            post(s, op, parent, -op.boundary_mg, "sorting boundary consumed; outputs/removals/difference separately accounted", user)
            destinations = {d.stream: d for d in s.scalars(select(Destination).filter_by(operation_id=op.id))}
            s.flush()
            for stream in ("selected", "pass_through", "remainder", "retained"):
                amount = quantities[stream]
                baseline = prior.get(stream, {})
                existing_parent = None
                if baseline.get("existing_portion_id"):
                    if added[stream] == 0:
                        get(s, Portion, baseline["existing_portion_id"]).status = "stock"
                        continue
                    existing_parent = consume_prior(s, user, op, stream, baseline)
                    s.flush()
                if not amount:
                    continue
                container_id = original.container_id if stream == "remainder" else destinations[stream].container_id if stream in destinations else None
                if container_id:
                    container = get(s, Container, container_id)
                    limit = container.capacity_mg
                    if (stream == "remainder" or (returning and stream == "pass_through")) and limit is not None:
                        # Material left in the same source vessel may retain its
                        # existing overfill; this does not authorize a new load.
                        limit = max(limit, op.boundary_mg)
                    if limit is not None and amount > limit:
                        from .sort_capacity import final_overfill_allowed
                        if final_overfill_allowed(s,user,op,container,amount,requested=data.get("capacity_override") is True):
                            limit = amount
                    if active_assignment(s, container_id=container_id) or (limit is not None and amount > limit):
                        raise DomainError("Destination occupied or capacity exceeded")
                portion = Portion(id=identity("portion"), created_by=op.id, status="wip" if stream == "retained" else "stock",
                                  quality="held", station_id=op.station_id if stream == "retained" else None)
                s.add(portion)
                s.flush()
                s.add(Lineage(parent_id=parent.id, child_id=portion.id, operation_id=op.id))
                if existing_parent:
                    s.add(Lineage(parent_id=existing_parent.id, child_id=portion.id, operation_id=op.id))
                s.add(Output(operation_id=op.id, portion_id=portion.id, stream=stream, mass_mg=amount))
                post(s, op, portion, amount, f"{stream} from sorting", user)
                if container_id:
                    s.add(Assignment(id=identity("assignment"), container_id=container_id, portion_id=portion.id, operation_id=op.id))
                if stream in {"selected", "pass_through"}:
                    profile = {**model_profile, "applies_to": stream, "mass_basis": "independent entered mass",
                               "membership_evidence": "calculated_prediction", "physical_output_count": None}
                else:
                    profile = {"schema": "beanosort-population-profile/v1", "coverage": "unknown",
                               "composition_evidence": "unknown", "count": None, "moisture": {"value": None, "evidence": "unknown"},
                               "note": "No observed membership or representativeness assumption for this remainder/retained material"}
                if weighing and stream in weighing["model"]["populations"]:
                    from .weighing import count_estimate
                    estimate = count_estimate(added[stream] if existing_parent else amount, weighing["model"], stream)
                    profile.update(mass_count_estimate=estimate, estimated_count=estimate["estimated_count"],
                                   count_per_kg=estimate["count_per_kg"], assumption_dependence_group=op.id)
                    if stream == "remainder" and estimate["mean_bean_mass_mg"]:
                        profile["note"] = ("Unobserved source remainder; count retains the source population's mean bean mass. Composition and physical membership remain estimates."
                            if weighing["model"].get("input_population_profile_id") else
                            "Unobserved source remainder; count assumes the reference-wide mean bean mass. Composition and physical membership remain unknown.")
                if existing_parent:
                    estimate_for_mix = {**estimate,"model":weighing["model"]} if weighing else None
                    profile = combined_profile(s, op, stream, baseline, amount, profile, estimate_for_mix, portion.id)
                if returning and stream == "pass_through":
                    profile.update(source_return=True, membership_evidence="inherited_estimate",
                        coverage="generated passages only; full container coverage unknown",
                        note="Source and Pass-through share a vessel. Final weight includes unprocessed source beans and returned passages; run observations are not a census of all contents.")
                s.add(Profile(id=identity("profile"), operation_id=op.id, portion_id=portion.id, stream=stream, version=1, data=profile))
            s.add(Profile(id=identity("profile"), operation_id=op.id, stream="run", version=1, data=model_profile))
            for model in (Reservation, Destination):
                for item in s.scalars(select(model).filter_by(operation_id=op.id, active=True)):
                    item.active = False
            op.release_status = "held"
            audit(s, user, "reconciled_held", {"reconciliation_id": recon.id, "discrepancy_mg": discrepancy,
                  "loss_mg": quantities["loss"], "sample_mg": quantities["sample"], "retained_mg": quantities["retained"]}, op)
            remember(s, key, payload, recon.id)
            return recon.id

    def release(self, user_id, operation_id, *, accept_estimates, reason, approve_discrepancy=False):
        from .handling import effective_outputs, effective_reconciliation
        from .models import Correction, HandlingPlan
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "supervisor", "admin")
            op = get(s, Operation, operation_id)
            if op.status == "completed":
                return
            recon = effective_reconciliation(s, op)
            if op.release_status != "held" or not recon:
                raise DomainError("Reconcile all quantities before release")
            policy = get(s, Version, op.policy_id).data
            if abs(recon.discrepancy_mg) > policy["tolerance_mg"]:
                if self.db.environment != "simulation" or op.kind != "sorting" or approve_discrepancy is not True:
                    raise DomainError("Unexplained difference exceeds this job's pinned policy; outputs remain held")
                audit(s, user, "simulation_discrepancy_approved", {"discrepancy_mg": recon.discrepancy_mg,
                      "policy_id": op.policy_id, "pinned_tolerance_mg": policy["tolerance_mg"],
                      "reason": reason, "evidence": "simulation", "classification": "unexplained difference, not waste"}, op)
            if not policy["allow_estimated_composition"] or accept_estimates is not True or not reason.strip():
                raise DomainError("Explicit approval of estimated/unknown composition and a reason are required")
            plan = s.scalar(select(HandlingPlan).filter_by(operation_id=op.id))
            if plan:
                evaluation = plan.data.get("specification_evaluation", {})
                achieved = s.scalar(select(Profile).filter_by(operation_id=op.id, stream="blend"))
                if achieved:
                    evaluation = achieved.data.get("specification_evaluation", evaluation)
                if evaluation.get("status") == "prediction_fail":
                    raise DomainError("Predicted blend fails its pinned specification; outputs remain held")
                if any(c["constraint"].get("required_evidence") and c["status"] != "prediction_pass" for c in evaluation.get("constraints", [])):
                    raise DomainError("Required specification evidence is missing; outputs remain held")
            for link in s.scalars(select(Input).filter_by(operation_id=op.id)):
                if get(s, Portion, link.portion_id).hold_reason:
                    raise DomainError("Source has an explicit quality hold; release requires later reviewed disposition")
            for output in effective_outputs(s, op):
                if get(s, Portion, output.portion_id).hold_reason:
                    raise DomainError("An output has an explicit quality hold")
                if output.stream != "retained":
                    get(s, Portion, output.portion_id).quality = "released"
            op.status = "completed"
            op.release_status = "released"
            if isinstance(recon, Correction):
                corrected = get(s, Operation, recon.operation_id)
                corrected.status, corrected.release_status = "completed", "released"
            audit(s, user, "released", {"policy_id": op.policy_id, "accepted_estimated_or_unknown_composition": True,
                  "reason": reason, "retained_material": "remains held at station", "product_compliance": "not established"}, op)

    def set_hold(self, user_id, portion_id, reason):
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "supervisor", "admin")
            portion = get(s, Portion, portion_id)
            if not reason.strip():
                raise DomainError("Hold requires a reason")
            portion.quality = "held"
            portion.hold_reason = reason
            audit(s, user, "quality_hold", {"portion_id": portion_id, "reason": reason}, get(s, Operation, portion.created_by))


def trace(session, portion_id):
    """All ancestors and descendants, avoiding unrelated siblings."""
    edges = list(session.scalars(select(Lineage)))
    found = {portion_id}
    for direction in ("back", "forward"):
        pending, seen = [portion_id], {portion_id}
        while pending:
            key = pending.pop()
            for edge in edges:
                source, dest = (edge.child_id, edge.parent_id) if direction == "back" else (edge.parent_id, edge.child_id)
                if source == key and dest not in seen:
                    seen.add(dest)
                    pending.append(dest)
        found |= seen
    return [get(session, Portion, key) for key in sorted(found)], [e for e in edges if e.parent_id in found and e.child_id in found]
