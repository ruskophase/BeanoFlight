"""Read-only job captions using recorded container roles, not current placement."""
from sqlalchemy import or_, select

from .models import Assignment, Audit, Container, Destination, HandlingPlan, Input, Operation, Version


def describe_operation(session, operation_id):
    operation = session.get(Operation, operation_id)
    if operation is None:
        return {"recipe": "Operation history unavailable", "roles": []}
    record = session.scalar(select(HandlingPlan).where(HandlingPlan.operation_id == operation.id))
    if record and record.data.get("schema") == "beanosort-material-record/v1":
        data = record.data
        if operation.kind == "product_packaging":
            return {"recipe": "Package as product", "caption_label": "Action", "mass_note": "After packaging",
                    "roles": [("Container", data["container"]["label"]), ("Final product", "Removed from inventory")],
                    "final_masses_mg": {"Container": data["after_mg"], "Final product": data["packaged_mg"]}}
        if operation.kind == "contents_move":
            return {"recipe": "Move contents", "caption_label": "Action", "mass_note": "Balances after movement",
                    "roles": [("Source", data["source"]["label"]), ("Destination", data["destination"]["label"])],
                    "final_masses_mg": {"Source": data["source_after_mg"], "Destination": data["destination_after_mg"]}}
        return {"recipe": "Reconciliation", "caption_label": "Action", "mass_note": "Net contents after weighing",
                "roles": [("Container", data["container"]["label"])],
                "final_masses_mg": {"Container": data["after_mg"]}}
    recipe = session.get(Version, operation.recipe_id) if operation.recipe_id else None
    final_masses = {}
    if operation.kind == "sorting" and operation.status == "completed":
        from .handling import effective_reconciliation
        reconciliation = effective_reconciliation(session, operation)
        if reconciliation:
            # Totals at this boundary include any starting destination stock.
            # Source means material left in its vessel, not the input feed mass.
            final_masses = {role: reconciliation.accounting.get(key) for role, key in (
                ("Source", "remainder_mg"), ("Selected", "selected_mg"), ("Pass-through", "pass_through_mg"))}

    def container_name(container_id):
        container = session.get(Container, container_id) if container_id else None
        if container:
            return container.label + (" (deleted)" if container.deleted else "")
        return "Not recorded" if not container_id else container_id

    def historical_sources():
        inputs = list(session.scalars(select(Input.portion_id).where(Input.operation_id == operation.id)))
        # A transfer retains material identity; later placements must not rename
        # the container that held an input when this operation was prepared.
        labels = []
        for portion_id in inputs:
            assignments = list(session.scalars(select(Assignment).where(Assignment.portion_id == portion_id,
                Assignment.created_at <= operation.created_at,
                or_(Assignment.ended_at.is_(None), Assignment.ended_at >= operation.created_at))))
            label = container_name(assignments[0].container_id) if len(assignments) == 1 else "Not recorded"
            if label not in labels:
                labels.append(label)
        return ", ".join(labels) or "Not recorded"

    if operation.kind == "sorting":
        from .sorting_modes import hopper_mode, source_return
        preparation = session.scalar(select(Audit).where(Audit.operation_id == operation.id,
            Audit.action == "job_prepared").order_by(Audit.created_at).limit(1))
        recorded = preparation.data if preparation else {}
        destinations = {d.stream: d.container_id for d in session.scalars(select(Destination).where(Destination.operation_id == operation.id))}
        requested = recorded.get("requested_destinations", {})
        # Missing legacy preparation evidence stays unknown instead of treating
        # previous destination contents as sorter feed in a multi-input boundary.
        source = container_name(recorded["source_container_id"]) if recorded.get("source_container_id") else "Not recorded"
        roles = [("Source", source),
                 ("Selected", container_name(destinations.get("selected") or requested.get("selected"))),
                 ("Pass-through", container_name(destinations.get("pass_through") or requested.get("pass_through")))]
        if hopper_mode(session, operation):
            roles = [item for item in roles if item[0] != "Selected"]
            if source_return(session, operation):
                roles = [("Source / Pass-through", source)]
                final_masses = ({"Source / Pass-through":final_masses["Pass-through"]} if final_masses else {})
        from .segments import current,collections
        if current(session,operation):
            items=collections(session,operation)
            alias=any(session.get(Destination,item.destination_id).readiness.get("source_return") for item in items)
            roles=[] if alias else [("Source",source)]
            collection_masses=reconciliation.accounting.get("collection_masses_mg",{}) if operation.status=="completed" and reconciliation else {}
            final_masses=({"Source":reconciliation.accounting.get("remainder_mg")} if collection_masses and not alias else {})
            for item in items:
                shared=session.get(Destination,item.destination_id).readiness.get("source_return")
                role=("Source / Pass-through" if shared else item.stream.replace("_"," ").title())+f" {item.ordinal}"
                roles.append((role,container_name(item.container_id)))
                if item.code in collection_masses:final_masses[role]=collection_masses[item.code]
    elif operation.kind == "receiving":
        assignments = session.scalars(select(Assignment).where(Assignment.operation_id == operation.id))
        names = sorted({container_name(a.container_id) for a in assignments})
        roles = [("Receiving containers", ", ".join(names) or "Not recorded")]
    else:
        roles = [("Source", historical_sources())]
        plan = session.scalar(select(HandlingPlan).where(HandlingPlan.operation_id == operation.id))
        if plan:
            roles.append(("Output", container_name(plan.data.get("destination_id"))))
    return {"recipe": f"{recipe.name} (v{recipe.version})" if recipe else "No sorting recipe",
            "roles": roles, "final_masses_mg": final_masses}
