"""Current container captions shared by every container dropdown."""
from sqlalchemy import func, select

from .models import Assignment, Destination, Ledger, Operation
from .quantities import kg3


def option_labeler(session):
    # Use total recorded contents, including reserved material. Historical
    # assignments must not contribute to a container's current contents.
    masses = {container_id: int(value) for container_id, value in session.execute(
        select(Assignment.container_id, func.coalesce(func.sum(Ledger.delta_mg), 0))
        .outerjoin(Ledger, Ledger.portion_id == Assignment.portion_id)
        .where(Assignment.active == True).group_by(Assignment.container_id))}
    contents = {container_id: f"{kg3(value)} kg contents" for container_id, value in masses.items()}

    # Sorting additions have no stock ledger entry until reconciliation. Do not
    # describe a collecting vessel as empty just because its ledger is still zero.
    collecting = session.scalars(select(Destination.container_id).join(
        Operation, Operation.id == Destination.operation_id).where(
        Destination.active == True, Operation.kind == "sorting",
        Operation.status.in_(["running", "paused", "failed", "finishing", "awaiting_reconciliation"])))
    for container_id in collecting:
        from .collection_views import for_container
        collection = for_container(session, container_id)
        if collection and collection["net_mg"] is not None:
            contents[container_id] = f"{kg3(collection['net_mg'])} kg contents (last weighed)"
        else:
            contents[container_id] = f"contents awaiting weighing ({kg3(masses.get(container_id, 0))} kg previously recorded)"

    def label(container):
        capacity = (f"nominal capacity {kg3(container.capacity_mg)} kg"
                    if container.capacity_mg is not None else "nominal capacity not recorded")
        return f"{container.label} · {container.location} · {contents.get(container.id, '0.000 kg contents')} · {capacity}"

    return label
