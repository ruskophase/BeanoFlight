"""Occupied sorting destinations: reservations, total weighing and combination.

Existing destination stock is not sorter feed. It is an additional material input
only when reconciliation records a positive net addition to that container.
"""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from functools import reduce
from math import gcd, lcm

from sqlalchemy import func, select

from .models import Container, Destination, Input, Particle, Portion, ProductLot, Profile, Reservation, identity, now
from .quantities import DomainError


def baselines(session, operation):
    result = {}
    for d in session.scalars(select(Destination).filter_by(operation_id=operation.id)):
        ready = d.readiness
        # The occupied-destination DB guard still pins the source assignment and
        # its one full reservation. It is not an additional inventory input.
        result[d.stream] = ({**ready, "source_portion_id":ready["existing_portion_id"],
            "source_mass_mg":ready["existing_mass_mg"], "existing_portion_id":None,
            "existing_mass_mg":0} if ready.get("source_return") else ready)
    return result


def feed_input(session, operation):
    prior = {b.get("existing_portion_id") for b in baselines(session, operation).values()}
    inputs = list(session.scalars(select(Input).where(Input.operation_id == operation.id, Input.portion_id.not_in(prior - {None}))))
    if len(inputs) != 1:
        raise DomainError("Sorting needs exactly one identified feed population")
    return inputs[0]


def starting_mass(session, operation):
    return sum(b.get("existing_mass_mg", 0) for b in baselines(session, operation).values())


def eligible_destination(session, container, *, operation_id=None):
    from .service import active_assignment, available, balance
    if container.deleted or container.archived:
        return False
    claim = session.scalar(select(Destination).filter_by(container_id=container.id, active=True))
    if claim and claim.operation_id != operation_id:
        return False
    assigned = active_assignment(session, container_id=container.id)
    if not assigned:
        return True
    portion = session.get(Portion, assigned.portion_id)
    if portion.status != "stock" or portion.quality != "released" or portion.hold_reason or session.scalar(select(ProductLot.id).filter_by(portion_id=portion.id)):
        return False
    own = session.scalar(select(Reservation).filter_by(portion_id=portion.id, operation_id=operation_id, active=True)) if operation_id else None
    return balance(session, portion.id) > 0 and (available(session, portion) == balance(session, portion.id) or bool(own))


def pin_destination(session, user, operation, container_id, stream, *, dataset=None, settings=None, source_return=False, logical_stream=None):
    from .service import active_assignment, balance, get
    container = get(session, Container, container_id)
    if source_return:
        from .sorting_modes import hopper_mode
        assignment = active_assignment(session, container_id=container.id)
        feed = feed_input(session, operation)
        reservation = session.scalar(select(Reservation).filter_by(operation_id=operation.id, portion_id=feed.portion_id, active=True))
        if (not hopper_mode(session, operation) or (logical_stream or stream) != "pass_through" or not assignment
                or assignment.portion_id != feed.portion_id or not reservation or reservation.mass_mg != feed.mass_mg):
            raise DomainError("Only the hopper-emptying recipe may return its reserved source to Pass-through")
        session.add(Destination(id=identity("destination"), operation_id=operation.id, container_id=container.id,
            stream=stream, readiness={"source_return":True, "ready_to_receive":True, "operator_id":user.id,
                "at":now(), "container_revision":container.revision, "existing_portion_id":feed.portion_id,
                "existing_assignment_id":assignment.id, "existing_mass_mg":feed.mass_mg}))
        return
    if not eligible_destination(session, container):
        raise DomainError("Destination must be empty or contain released, unreserved bulk material")
    ready = {"ready_to_receive": True, "operator_id": user.id, "at":now(), "container_revision": container.revision,
             "existing_mass_mg": 0}
    assignment = active_assignment(session, container_id=container.id)
    if assignment:
        portion = get(session, Portion, assignment.portion_id)
        quantity = balance(session, portion.id)
        profile = session.scalar(select(Profile).filter_by(portion_id=portion.id).order_by(Profile.created_at.desc(), Profile.version.desc()))
        ready.update(existing_portion_id=portion.id, existing_assignment_id=assignment.id,
                     existing_mass_mg=quantity, existing_profile_id=profile.id if profile else None)
        if dataset:
            from .population_sources import source_snapshot
            try:
                snapshot = source_snapshot(session, portion, dataset, settings, quantity, force=True)
            except DomainError as exc:
                ready["population_limitation"] = str(exc)
            else:
                snapshot_id = "collection_"+stream
                session.add(Profile(id=identity("profile"), operation_id=operation.id, stream=snapshot_id,
                                    version=1, data=snapshot))
                session.flush()
                ready["population_profile_id"] = session.scalar(select(Profile.id).filter_by(operation_id=operation.id, stream=snapshot_id))
        session.add(Reservation(id=identity("reservation"), operation_id=operation.id, portion_id=portion.id, mass_mg=quantity))
        session.flush()
    else:
        ready["empty_clean_ready"] = True
    session.add(Destination(id=identity("destination"), operation_id=operation.id, container_id=container.id,
                            stream=stream, readiness=ready))


def check_destination_contents(session, operation, *, start=False):
    from .service import active_assignment, balance, get
    for destination in session.scalars(select(Destination).filter_by(operation_id=operation.id, active=True)):
        baseline = destination.readiness
        if baseline.get("source_return"):
            from .sorting_modes import hopper_mode
            from .segments import for_output
            collection=for_output(session,operation.id,destination.stream)
            if not hopper_mode(session, operation) or (collection.stream if collection else destination.stream) != "pass_through" or feed_input(session, operation).portion_id != baseline.get("existing_portion_id"):
                raise DomainError("Invalid source-return destination")
        assigned = active_assignment(session, container_id=destination.container_id)
        pid = baseline.get("existing_portion_id")
        if not pid:
            if assigned: raise DomainError("An empty destination was filled after preparation")
            continue
        portion = get(session, Portion, pid)
        reservation = session.scalar(select(Reservation).filter_by(operation_id=operation.id, portion_id=pid, active=True))
        if not assigned or assigned.id != baseline["existing_assignment_id"] or assigned.portion_id != pid or balance(session, pid) != baseline["existing_mass_mg"]:
            raise DomainError("Destination contents changed; review this job before processing")
        if not reservation or portion.status not in {"stock", "wip"}:
            raise DomainError("Destination contents are held or no longer reserved for this job")
        if start and (portion.quality != "released" or portion.hold_reason):
            raise DomainError("Destination contents have been placed on hold; feeding cannot start or resume")
        if start: portion.status = "wip"


def additions(session, operation, quantities):
    prior = baselines(session, operation)
    result = {}
    for stream in ("selected", "pass_through"):
        baseline = prior.get(stream, {}).get("existing_mass_mg", 0)
        if quantities[stream] < baseline:
            raise DomainError("Final destination mass is below its starting contents; record a reviewed removal/adjustment before using this addition workflow")
        result[stream] = quantities[stream]-baseline
    return result


def consume_prior(session, user, operation, stream, baseline):
    from .service import active_assignment, get, now, post, audit
    parent = get(session, Portion, baseline["existing_portion_id"])
    assignment = active_assignment(session, portion_id=parent.id)
    session.add(Input(operation_id=operation.id, portion_id=parent.id, mass_mg=baseline["existing_mass_mg"]))
    post(session, operation, parent, -baseline["existing_mass_mg"], "Existing destination contents combined with sorted addition", user)
    parent.status = "consumed"; assignment.active = False; assignment.ended_at = now()
    audit(session, user, "destination_population_combined", {"stream":stream, "previous_portion_id":parent.id,
        "starting_mass_mg":baseline["existing_mass_mg"], "new_population_created":True,
        "mixing_uniformity":"not verified"}, operation)
    return parent


def preview_count(session, baseline, net_mg, incoming):
    old=session.get(Profile,baseline.get("existing_profile_id")) if baseline.get("existing_profile_id") else None
    pool=session.get(Profile,baseline.get("population_profile_id")) if baseline.get("population_profile_id") else None
    mean=pool.data["mass_model"]["input_mean_bean_mass_mg"] if pool else None
    density=old.data.get("count_per_kg") if old else None
    before=Decimal(baseline["existing_mass_mg"])/Decimal(mean) if mean else Decimal(baseline["existing_mass_mg"])*Decimal(density)/1000000 if density else None
    added=Decimal(incoming["estimated_count"]) if incoming["estimated_count"] is not None else None
    total=before+added if before is not None and added is not None else None
    return {**incoming,"estimated_count":str(total) if total is not None else None,"mass_basis_mg":net_mg,
            "assumption":"sum of inherited starting count and estimated new addition; no physical census"}


def combined_profile(session, operation, stream, baseline, final_mg, incoming_profile, incoming_estimate, portion_id, *, collection=None):
    old = session.get(Profile, baseline.get("existing_profile_id")) if baseline.get("existing_profile_id") else None
    pool = session.get(Profile, baseline.get("population_profile_id")) if baseline.get("population_profile_id") else None
    before_mg = baseline["existing_mass_mg"]; added_mg = final_mg-before_mg
    old_mean = pool.data["mass_model"]["input_mean_bean_mass_mg"] if pool else None
    old_density = old.data.get("count_per_kg") if old else None
    old_count = Decimal(before_mg)/Decimal(old_mean) if old_mean else Decimal(before_mg)*Decimal(old_density)/1000000 if old_density else None
    new_count = Decimal(incoming_estimate["estimated_count"]) if incoming_estimate and incoming_estimate["estimated_count"] is not None else None
    total = old_count+new_count if old_count is not None and new_count is not None else None
    result = {"schema":"beanosort-population-profile/v2", "coverage":"unknown", "composition_evidence":"calculated_prediction",
              "moisture":{"value":None,"evidence":"unknown"}, "mixing_uniformity":"not verified",
              "estimated_count":str(total) if total is not None else None,
              "count_per_kg":str(total*1000000/final_mg) if total is not None else None,
              "destination_combination":{"previous_portion_id":baseline["existing_portion_id"],
                  "previous_profile_id":baseline.get("existing_profile_id"),"before_mg":before_mg,"added_mg":added_mg,
                  "final_mg":final_mg,"new_mass_evidence":"calculated difference of final net mass and pinned starting stock",
                  "incoming_run_id":incoming_profile.get("run_id"),"incoming_stream":stream,
                  "source_versions":incoming_profile.get("source_versions",{})},
              "note":"Collection combined existing stock and a sorted addition. Mass contributions do not establish source percentages by count or uniform mixing."}
    if pool and total and new_count and incoming_estimate.get("mean_bean_mass_mg"):
        run_id = incoming_profile.get("run_id")
        seq = Particle.payload["reference_bean"]["bean_sequence"].as_integer()
        query=select(seq,func.count(Particle.id)).where(Particle.run_id==run_id,Particle.simulated_destination==stream)
        if collection:
            from .segments import particle_filter
            condition=particle_filter(session,collection)
            if condition is None:
                query=query.where(False)
            else:query=query.where(condition)
        added_weights = [[key,count] for key,count in session.execute(query.group_by(seq).order_by(seq))]
        source = pool.data
        incoming_model = incoming_estimate.get("model") or {}
        if added_weights and incoming_model.get("mean_bean_mass_ug") and Decimal(source["mass_model"]["reference_mean_mass_ug"]) == Decimal(incoming_model["mean_bean_mass_ug"]):
            before = source["weights"]
            weights = {}
            for values,count in ((before,old_count),(added_weights,new_count)):
                denominator=sum(weight for _,weight in values)
                for key,weight in values:weights[key]=weights.get(key,Fraction(0))+Fraction(count)*weight/denominator
            denominator=reduce(lcm,(v.denominator for v in weights.values()),1)
            integer={key:v.numerator*(denominator//v.denominator) for key,v in weights.items()}
            divisor=reduce(gcd,integer.values())
            combined=[[key,integer[key]//divisor] for key in sorted(integer)]
            model=deepcopy(source["mass_model"]);model["input_mean_bean_mass_mg"]=str(Decimal(final_mg)/total)
            result["combined_population"]={"schema":"beanosort-combined-population/v1","dataset_id":source["dataset_id"],
                "dataset_fingerprint":source["dataset_fingerprint"],"weights":combined,"before_weights":before,
                "added_weights":added_weights,"before_count":str(old_count),"added_count":str(new_count),"combined_count":str(total),
                "mass_model":model,"membership_evidence":"inherited_estimate","physical_confirmation":False}
            result["population_analysis_url"]=f"/jobs/{operation.id}/populations?population={stream}&contents={portion_id}"
    if "combined_population" not in result:
        result["composition_evidence"] = "unknown"
        result["note"] += " Joint composition cannot be established from the available compatible population evidence."
    return result


def collection_members(collection, population):
    key={"input":"before","selected":"combined","pass_through":"added"}[population]
    weights=collection["weights"] if key=="combined" else collection[key+"_weights"]
    count=Decimal(collection[key+"_count"]);total=sum(weight for _,weight in weights)
    return [(seq,count*Decimal(weight)/total) for seq,weight in weights] if total else []


def collection_report(collection,dataset,recipe,model):
    from .references import features, load_dataset, population_report
    report=population_report([],dataset,recipe,model);beans,_=load_dataset(dataset)
    by_sequence={bean["bean_sequence"]:bean for bean in beans}
    for population,series in (("input","input"),("selected","expected_selected"),("pass_through","expected_pass_through")):
        target=report["populations"][series];target["count"]="0"
        histograms={key:[Decimal(0)]*len(values) for key,values in target["histograms"].items()};unknown={key:Decimal(0) for key in target["unknown"]}
        total=Decimal(0)
        for seq,count in collection_members(collection,population):
            total+=count;values=features(by_sequence[seq],report["definitions"])
            for key,spec in report["definitions"].items():
                value=values[key];bucket=next((i for i,(lo,hi) in enumerate(zip(spec['edges'],spec['edges'][1:])) if value is not None and lo<=value<hi),None)
                if bucket is None:unknown[key]+=count
                else:histograms[key][bucket]+=count
        target.update(count=str(total),histograms={key:list(map(str,values)) for key,values in histograms.items()},unknown={key:str(value) for key,value in unknown.items()})
    report.update(combination=True,series_titles={"input":"Before addition","selected":"Combined contents","pass_through":"Added this run"},
                  basis="count-weighted combination estimate; inherited simulation membership and assumed count per mass; not a physical census")
    return report
