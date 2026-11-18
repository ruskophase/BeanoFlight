"""Read-only descriptions of a portion's recorded creation history."""
from sqlalchemy import and_, select

from .models import Destination, Lineage, Operation, Output, Portion, Version


def describe_portion(session, portion_id, cache=None, seen=frozenset()):
    cache = {} if cache is None else cache
    if portion_id in cache:
        return cache[portion_id]
    if portion_id in seen or len(seen) >= 100:
        return "Creation history unavailable"
    row = session.execute(select(Operation.kind, Version.name, Version.version, Output.stream, Destination.readiness)
        .select_from(Portion).join(Operation, Portion.created_by == Operation.id)
        .outerjoin(Version, Operation.recipe_id == Version.id)
        .outerjoin(Output, and_(Output.operation_id == Operation.id, Output.portion_id == Portion.id))
        .outerjoin(Destination, and_(Destination.operation_id == Operation.id, Destination.stream == Output.stream))
        .where(Portion.id == portion_id)).first()
    if row is None:
        return "Creation history unavailable"
    kind, recipe, version, stream, readiness = row
    if kind == "sorting":
        from .segments import for_output
        owner=session.get(Portion,portion_id)
        collection=for_output(session,owner.created_by,stream)
        if collection:stream=collection.stream
        source = f"{recipe} (v{version})" if recipe else "Unknown sorting recipe"
        output = {"selected": "Selected", "pass_through": "Pass-through",
                  "remainder": "Unprocessed remainder", "retained": "Retained in machine"}.get(stream, "Unknown output")
        if readiness and readiness.get("source_return"):
            output += " · Source returned to same container"
        elif readiness and readiness.get("existing_portion_id"):
            output += " addition · Combined with existing contents"
        description = f"{source} · {output}"
        if collection:description+=f" · collection {collection.ordinal}"
    elif kind == "receiving":
        description = "Received material · No sorting recipe"
    else:
        parents = list(session.scalars(select(Lineage.parent_id).where(Lineage.child_id == portion_id)))
        action = {"split": "Split remainder" if stream == "remainder" else "Split portion",
                  "contents_move": "Movement remainder" if stream == "remainder" else "Combined contents" if stream == "combined" else "Moved contents",
                  "weight_adjustment": "Previously unrecorded contents",
                  "adjustment": "Measurement correction", "packaging": "Packaged"}.get(kind, kind.replace("_", " ").title())
        if len(parents) == 1:
            description = describe_portion(session, parents[0], cache, seen | {portion_id}) + " · " + action
        else:
            description = action + " · See contributing population histories"
    cache[portion_id] = description
    return description
