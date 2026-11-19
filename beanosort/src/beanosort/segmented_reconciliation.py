"""Final multi-container accounting, using the existing material ledger."""
from sqlalchemy import select

from .models import (Assignment, Container, Destination, Input, Lineage, Output, Portion, Profile,
                     Reconciliation, Reservation, identity, now)
from .quantities import DomainError,mass
from .segments import collections,current


def reconcile(workflow,session,user,operation,key,payload,data):
    from .collections import baselines,check_destination_contents,combined_profile,consume_prior,feed_input,starting_mass
    from .service import active_assignment,audit,get,measure,post,remember
    from .sorting_modes import source_return
    from .weighing import count_estimate,validate_and_record_tares
    items=collections(session,operation);codes={item.code for item in items}
    entered=data.get("collection_masses")
    if not isinstance(entered,dict) or set(entered)!=codes:
        raise DomainError("Enter a final mass for every collection used in this job")
    quantities={code:mass(entered[code],zero=True) for code in codes}
    for name in ("remainder","retained","loss","sample"):
        quantities[name]=mass(data.get(name,""),zero=True)
    returning=source_return(session,operation)
    if returning and quantities["remainder"]:
        raise DomainError("Source remainder is included in the shared collection's one weighing")
    if quantities["sample"]:raise DomainError("Sampling is not used by container reconciliation")
    prior=baselines(session,operation);added={}
    for item in items:
        baseline=prior[item.code].get("existing_mass_mg",0)
        if quantities[item.code]<baseline:raise DomainError("Final contents cannot be below pinned starting stock in this addition workflow")
        added[item.code]=quantities[item.code]-baseline
    check_destination_contents(session,operation)
    parent=get(session,Portion,feed_input(session,operation).portion_id)
    original=active_assignment(session,portion_id=parent.id)
    if parent.status!="wip" or not original:raise DomainError("Source must stay assigned and in work-in-progress")
    total=operation.boundary_mg+starting_mass(session,operation)
    discrepancy=total-sum(quantities.values())
    if not str(data.get("reason","")).strip():raise DomainError("Record weighing/accounting notes")
    proof=data.get("container_weighing")
    if not proof or proof.get("discrepancy_mg")!=discrepancy:raise DomainError("Review all container weights and the current discrepancy")
    if proof["model"].get("available") and workflow.db.environment!="simulation":
        raise DomainError("Simulation predictions cannot become real measurements")
    validate_and_record_tares(session,user,operation,original,proof,quantities,workflow.db.environment)
    for role,quantity in quantities.items():
        evidence=dict(data["evidence"])
        if role in proof["containers"]:
            entry=proof["containers"][role]
            evidence.update(container_id=entry["container_id"],gross_mg=entry["gross_mg"],tare_mg=entry["tare_mg"],
                container_revision=entry["effective_tare_revision"],weighed_confirmation=True,
                collection_id=entry.get("collection_id"),previous_weighing_id=entry.get("saved_weight_id"),
                previous_weighing_reused=entry.get("saved_weight_reused",False),
                starting_mass_mg=entry.get("starting_mass_mg",0),net_evidence="calculated_difference: gross minus tare")
        else:evidence.update(method="operator-declared removal/retention or unused shared-source remainder")
        measure(session,operation,role,quantity,evidence,user,workflow.db.environment)
    final={item.code:quantities[item.code] for item in items}
    accounting={"input_mg":total,"feed_input_mg":operation.boundary_mg,"collection_masses_mg":final,
        "collection_ids":{item.code:item.id for item in items},
        "selected_mg":sum(final[item.code] for item in items if item.stream=="selected"),
        "pass_through_mg":sum(final[item.code] for item in items if item.stream=="pass_through"),
        **{name+"_mg":quantities[name] for name in ("remainder","retained","loss","sample")},
        "destination_starting_mg":{code:value.get("existing_mass_mg",0) for code,value in prior.items()},
        "unexplained_difference_mg":discrepancy,"container_weighing":proof,"final_segment_id":current(session,operation).id}
    recon=Reconciliation(id=identity("reconciliation"),operation_id=operation.id,discrepancy_mg=discrepancy,
        accounting=accounting,reason=data["reason"],policy_id=operation.policy_id)
    session.add(recon)
    parent.status="consumed";original.active=False;original.ended_at=now()
    post(session,operation,parent,-operation.boundary_mg,"sorting boundary consumed; collection outputs independently reconciled",user)
    session.flush()
    for item in [*items,"remainder","retained"]:
        is_collection=not isinstance(item,str)
        code=item.code if is_collection else item;amount=quantities[code]
        baseline=prior.get(code,{})
        destination=session.get(Destination,item.destination_id) if is_collection else None
        shared=bool(destination and destination.readiness.get("source_return"))
        existing_parent=None
        if baseline.get("existing_portion_id"):
            if added[code]==0:
                old=get(session,Portion,baseline["existing_portion_id"]);old.status="stock"
                item.portion_id=old.id
                continue
            existing_parent=consume_prior(session,user,operation,code,baseline);session.flush()
        if not amount:continue
        container_id=item.container_id if is_collection else original.container_id if code=="remainder" else None
        if container_id:
            container=get(session,Container,container_id);limit=container.capacity_mg
            if (code=="remainder" or shared) and limit is not None:limit=max(limit,operation.boundary_mg)
            if limit is not None and amount>limit:
                from .sort_capacity import final_overfill_allowed
                if final_overfill_allowed(session,user,operation,container,amount,requested=data.get("capacity_override") is True):limit=amount
            if active_assignment(session,container_id=container_id) or limit is not None and amount>limit:
                raise DomainError("Destination occupied or capacity exceeded")
        portion=Portion(id=identity("portion"),created_by=operation.id,status="wip" if code=="retained" else "stock",
                        quality="held",station_id=operation.station_id if code=="retained" else None)
        session.add(portion);session.flush()
        session.add(Lineage(parent_id=parent.id,child_id=portion.id,operation_id=operation.id))
        if existing_parent:session.add(Lineage(parent_id=existing_parent.id,child_id=portion.id,operation_id=operation.id))
        session.add(Output(operation_id=operation.id,portion_id=portion.id,stream=code,mass_mg=amount))
        post(session,operation,portion,amount,code+" from segmented sorting",user)
        if container_id:session.add(Assignment(id=identity("assignment"),container_id=container_id,portion_id=portion.id,operation_id=operation.id))
        profile=workflow.profile(session,operation,collection=item) if is_collection else {
            "schema":"beanosort-population-profile/v2","composition_evidence":"inherited_estimate","coverage":"unknown",
            "moisture":{"value":None,"evidence":"unknown"},"note":"Unobserved source remainder/retained material; no physical census"}
        estimate=count_estimate(added[code] if existing_parent else amount,proof["model"],code) if code in proof["model"]["populations"] else None
        if estimate:profile.update(mass_count_estimate=estimate,estimated_count=estimate["estimated_count"],count_per_kg=estimate["count_per_kg"])
        if is_collection:
            item.portion_id=portion.id
            profile.update(collection_id=item.id,collection_code=code,logical_stream=item.stream,applies_to=item.stream)
            if existing_parent:
                profile=combined_profile(session,operation,item.stream,baseline,amount,profile,
                    {**estimate,"model":proof["model"]} if estimate else None,portion.id,collection=item)
                profile.update(collection_id=item.id,collection_code=code,logical_stream=item.stream)
            elif "reference_population" in profile:
                profile["population_analysis_url"]=f"/jobs/{operation.id}/populations?population={item.stream}&collection_id={item.id}"
            if shared:
                profile.update(source_return=True,membership_evidence="inherited_estimate",coverage="collection-window observations; full shared-vessel coverage unknown",
                    note="Final contents include any unprocessed source beans. Chart observations do not census the whole vessel.")
        session.add(Profile(id=identity("profile"),operation_id=operation.id,portion_id=portion.id,stream=code,version=1,data=profile))
    session.add(Profile(id=identity("profile"),operation_id=operation.id,stream="run",version=1,data=workflow.profile(session,operation)))
    for model in (Reservation,Destination):
        for record in session.scalars(select(model).filter_by(operation_id=operation.id,active=True)):record.active=False
    operation.release_status="held"
    audit(session,user,"reconciled_held",{"reconciliation_id":recon.id,"discrepancy_mg":discrepancy,
        "collections":final,"segments":current(session,operation).ordinal+1},operation)
    remember(session,key,payload,recon.id)
    return recon.id
