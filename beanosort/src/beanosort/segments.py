"""Container collection windows within a job; all commands are records only."""
from datetime import datetime, timezone

from sqlalchemy import and_, false, func, or_, select

from .auth import require_role
from .models import (Assignment, CollectionWeighing, Container, Destination, Operation, Particle,
                     Run, SortingCollection, SortingSegment, User, Version, identity)
from .quantities import DomainError, kg, mass


def current(session, operation):
    return session.scalar(select(SortingSegment).where(SortingSegment.operation_id == operation.id)
                          .order_by(SortingSegment.ordinal.desc()).limit(1))


def collections(session, operation):
    return list(session.scalars(select(SortingCollection).where(SortingCollection.operation_id == operation.id)
                               .order_by(SortingCollection.created_at, SortingCollection.id)))


def for_output(session, operation_id, code):
    return session.scalar(select(SortingCollection).filter_by(operation_id=operation_id, code=code))


def particle_filter(session, collection):
    """None means unknown association; never silently use all run particles."""
    periods = list(session.scalars(select(SortingSegment).where(SortingSegment.operation_id == collection.operation_id)
                                  .order_by(SortingSegment.ordinal)))
    if not periods or any(period.first_sequence is None for period in periods):
        return None
    windows = []
    for index, period in enumerate(periods):
        if getattr(period, collection.stream+"_id") != collection.id:
            continue
        condition = Particle.source_sequence >= period.first_sequence
        if index+1 < len(periods):
            condition = and_(condition, Particle.source_sequence < periods[index+1].first_sequence)
        windows.append(condition)
    return or_(*windows) if windows else false()


def latest_weight(session, collection):
    return session.scalar(select(CollectionWeighing).where(CollectionWeighing.collection_id == collection.id)
                          .order_by(CollectionWeighing.version.desc()).limit(1))


def is_open(segment, collection):
    return collection.id in {segment.selected_id, segment.pass_through_id}


def has_capacity(session, container):
    from .service import active_assignment, balance
    assignment = active_assignment(session, container_id=container.id)
    return container.capacity_mg is None or not assignment or balance(session, assignment.portion_id) < container.capacity_mg


def _collection(session, operation, destination, stream, ordinal):
    collection = SortingCollection(id=identity("collection"), operation_id=operation.id,
        destination_id=destination.id, container_id=destination.container_id, stream=stream,
        code=destination.stream, ordinal=ordinal)
    session.add(collection); session.flush()
    return collection


def _bootstrap(session, user, operation):
    linked = {}
    for destination in session.scalars(select(Destination).filter_by(operation_id=operation.id)):
        linked[destination.stream] = _collection(session, operation, destination, destination.stream, 1)
    segment = SortingSegment(id=identity("segment"), operation_id=operation.id, ordinal=0,
        first_sequence=0, recipe_id=operation.recipe_id, selected_id=linked["selected"].id if "selected" in linked else None,
        pass_through_id=linked["pass_through"].id, operator_id=user.id,
        evidence={"kind":"recorded_preparation", "method":"original assignments; no particle history rewritten"})
    session.add(segment);session.flush()
    return segment


def change_containers(workflow, user_id, key, operation_id, data):
    from .collections import check_destination_contents, eligible_destination, feed_input, pin_destination
    from .service import active_assignment, audit, get, remember, replay
    from .sorting_modes import hopper_mode
    payload = {"command":"change_sorting_containers", "operator":user_id, "operation_id":operation_id, "data":data}
    with workflow.db.write() as session:
        user=get(session,User,user_id);require_role(user,"operator","supervisor","admin")
        old=replay(session,key,payload)
        if old:return old
        operation=get(session,Operation,operation_id)
        if operation.kind != "sorting" or operation.status != "paused":
            raise DomainError("Pause this sorting job before changing containers")
        if any(data.get(name) is not True for name in ("feeding_stopped","in_flight_clear","ready")):
            raise DomainError("Confirm feeding stopped, in-flight beans cleared and replacement containers in place")
        if not str(data.get("reason", "")).strip():raise DomainError("Record why containers are being changed")
        run=session.scalar(select(Run).filter_by(operation_id=operation.id))
        if not run:raise DomainError("Start a run before using collection segments")
        check_destination_contents(session,operation)
        previous=current(session,operation)
        count,last=session.execute(select(func.count(Particle.id),func.max(Particle.source_sequence)).where(Particle.run_id==run.id)).one()
        minimum=0 if last is None else last+1
        if workflow.db.environment == "simulation":
            if count != minimum:raise DomainError("Recover missing simulation records before creating a collection boundary")
            cutoff=minimum
        else:
            value=data.get("first_sequence")
            try:cutoff=int(value) if value not in (None,"") else None
            except (ValueError,TypeError):raise DomainError("Enter a recorded local sequence or leave association unknown") from None
            if cutoff is not None and cutoff<minimum:raise DomainError("A boundary cannot retroactively move received particles")
        if previous and (previous.first_sequence is None and cutoff is not None or
                         previous.first_sequence is not None and cutoff is not None and cutoff<previous.first_sequence):
            raise DomainError("Collection boundaries cannot move backwards or silently repair unknown association")
        source_at=None
        if data.get("source_at"):
            try:
                timestamp=datetime.fromisoformat(data["source_at"])
                if timestamp.tzinfo is None:raise ValueError()
                source_at=timestamp.astimezone(timezone.utc).isoformat()
            except (ValueError,TypeError):raise DomainError("A source boundary time needs an explicit timezone") from None
        hopper=hopper_mode(session,operation)
        wanted={"pass_through":data.get("pass_through_id"), "selected":data.get("selected_id") or None}
        if not wanted["pass_through"] or (not hopper and not wanted["selected"]) or (hopper and wanted["selected"]):
            raise DomainError("Choose the output containers required by the pinned recipe")
        if wanted["selected"] and wanted["selected"]==wanted["pass_through"]:
            raise DomainError("Selected and Pass-through must use different containers")
        source=active_assignment(session,portion_id=feed_input(session,operation).portion_id)
        if not source:raise DomainError("Source assignment is unavailable")
        if not hopper and source.container_id in wanted.values():
            raise DomainError("A selective recipe cannot collect into its source vessel")
        if previous is None:previous=_bootstrap(session,user,operation)
        changed=False; linked={}
        for stream in ("selected","pass_through"):
            cid=wanted[stream]
            if not cid:continue
            prior_id=getattr(previous,stream+"_id")
            prior=session.get(SortingCollection,prior_id) if prior_id else None
            if prior and prior.container_id==cid:
                linked[stream]=prior.id;continue
            changed=True
            if session.scalar(select(SortingCollection.id).filter_by(operation_id=operation.id,container_id=cid)):
                raise DomainError("A container already used in this job cannot be reused before its contents are accounted for")
            container=get(session,Container,cid)
            returning=hopper and cid==source.container_id
            if not returning and not eligible_destination(session,container):
                raise DomainError("Replacement must be empty or contain released, unreserved material")
            if not returning and not has_capacity(session,container) and data.get("capacity_override") is not True:
                raise DomainError("Replacement container has no recorded capacity remaining")
            ordinal=(session.scalar(select(func.max(SortingCollection.ordinal)).filter_by(operation_id=operation.id,stream=stream)) or 0)+1
            code=stream+"_"+str(ordinal)
            pin_destination(session,user,operation,cid,code,logical_stream=stream,source_return=returning,
                dataset=None,settings=None)
            session.flush()
            destination=session.scalar(select(Destination).filter_by(operation_id=operation.id,stream=code))
            # Pin compatible simulation contents using the same recipe evidence.
            if not returning and workflow.db.environment=="simulation" and destination.readiness.get("existing_portion_id"):
                from .population_sources import source_snapshot
                from .references import validate_recipe
                from .models import Portion,Profile
                from .simulation import current_settings
                recipe=session.get(Version,operation.recipe_id)
                if recipe.data.get("schema")=="beanosort-reference-recipe/v1":
                    ready=dict(destination.readiness)
                    try:
                        snapshot=source_snapshot(session,session.get(Portion,ready["existing_portion_id"]),
                            validate_recipe(session,recipe.data),current_settings(session),ready["existing_mass_mg"],force=True)
                    except DomainError as exc:ready["population_limitation"]=str(exc)
                    else:
                        profile=Profile(id=identity("profile"),operation_id=operation.id,stream="collection_"+code,version=1,data=snapshot)
                        session.add(profile);session.flush();ready["population_profile_id"]=profile.id
                    destination.readiness=ready
            linked[stream]=_collection(session,operation,destination,stream,ordinal).id
        if not changed:raise DomainError("Choose at least one replacement container")
        segment=SortingSegment(id=identity("segment"),operation_id=operation.id,ordinal=previous.ordinal+1,
            first_sequence=cutoff,source_at=source_at,recipe_id=operation.recipe_id,selected_id=linked.get("selected"),
            pass_through_id=linked["pass_through"],operator_id=user.id,
            evidence={"kind":"simulation" if workflow.db.environment=="simulation" else "operator_declaration",
                "feeding_stopped":True,"in_flight_clear":True,"ready":True,"reason":data["reason"],
                "sequence_basis":"committed simulation prefix" if workflow.db.environment=="simulation" else "operator-entered local boundary" if cutoff is not None else "unknown",
                "hardware_action":"none"})
        session.add(segment);session.flush()
        from .run_summaries import initialize_collections
        initialize_collections(session,run,workflow.db.environment)
        check_destination_contents(session,operation,start=True)
        audit(session,user,"sorting_containers_changed",{"segment_id":segment.id,"first_sequence":cutoff,
            "previous_segment_id":previous.id,"collections":linked,"source_at":source_at,"confirmations":segment.evidence},operation)
        remember(session,key,payload,segment.id)
        return segment.id


def record_weight(workflow,user_id,key,collection_id,data):
    from .service import audit,get,remember,replay,check_revision
    payload={"command":"weigh_closed_collection","operator":user_id,"collection_id":collection_id,"data":data}
    with workflow.db.write() as session:
        user=get(session,User,user_id);require_role(user,"operator","supervisor","admin")
        old=replay(session,key,payload)
        if old:return old
        collection=get(session,SortingCollection,collection_id);operation=get(session,Operation,collection.operation_id)
        segment=current(session,operation);destination=get(session,Destination,collection.destination_id)
        if operation.status not in {"running","paused","finishing","failed","awaiting_reconciliation"} or is_open(segment,collection):
            raise DomainError("Weigh a removed collection, or use the final reconciliation for active bins")
        if destination.readiness.get("source_return"):
            raise DomainError("The source vessel must be weighed at final reconciliation because feeding may still change its contents")
        if data.get("weighed") is not True:raise DomainError("Confirm this container has been weighed")
        container=get(session,Container,collection.container_id);check_revision(container,data.get("revision"))
        if container.tare_mg is None:raise DomainError("Record its missing tare during final reconciliation")
        gross=mass(data.get("gross"),zero=True)
        if gross<container.tare_mg:raise DomainError("Gross weight cannot be below tare")
        if gross-container.tare_mg<destination.readiness.get("existing_mass_mg",0):raise DomainError("Final contents cannot be below pinned starting stock in this addition workflow")
        previous=latest_weight(session,collection)
        reading=CollectionWeighing(id=identity("weighing"),collection_id=collection.id,version=previous.version+1 if previous else 1,
            gross_mg=gross,tare_mg=container.tare_mg,container_revision=container.revision,operator_id=user.id,
            evidence={"kind":"simulation" if workflow.db.environment=="simulation" else "direct_measurement",
                "method":"operator-entered gross weight of removed collection","note":data.get("reason") or "Removed container weighed"})
        session.add(reading);session.flush()
        audit(session,user,"collection_weighed",{"collection_id":collection.id,"weighing_id":reading.id,
            "gross_mg":gross,"tare_mg":container.tare_mg},operation)
        remember(session,key,payload,reading.id);return reading.id


def movable_collection(session,container_id):
    collection=session.scalar(select(SortingCollection).join(Destination,Destination.id==SortingCollection.destination_id)
        .where(SortingCollection.container_id==container_id,Destination.active==True))
    if not collection:return False
    operation=session.get(Operation,collection.operation_id);segment=current(session,operation)
    destination=session.get(Destination,collection.destination_id)
    return bool(segment and not is_open(segment,collection) and not destination.readiness.get("source_return")
                and operation.status in {"running","paused","finishing","failed","awaiting_reconciliation"})
