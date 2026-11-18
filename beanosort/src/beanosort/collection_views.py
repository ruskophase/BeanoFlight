"""Operator projections of open/closed collection fills; no stock balances posted."""
from sqlalchemy import select
from .models import Container,Destination,Operation,Portion,Reconciliation,SortingCollection
from .segments import collections,current,is_open,latest_weight


def rows(session,operation):
    segment=current(session,operation)
    if not segment:return []
    reconciliation=session.scalar(select(Reconciliation).filter_by(operation_id=operation.id))
    result=[]
    for item in collections(session,operation):
        destination=session.get(Destination,item.destination_id);reading=latest_weight(session,item)
        result.append({"collection":item,"container":session.get(Container,item.container_id),
            "portion":session.get(Portion,item.portion_id) if item.portion_id else None,
            "open":is_open(segment,item),"source_alias":bool(destination.readiness.get("source_return")),
            "weight":reading,"net_mg":reading.gross_mg-reading.tare_mg if reading else None,
            "final_mg":reconciliation.accounting.get("collection_masses_mg",{}).get(item.code) if reconciliation else None,
            "starting_mg":0 if destination.readiness.get("source_return") else destination.readiness.get("existing_mass_mg",0)})
    return result


def for_container(session,container_id):
    item=session.scalar(select(SortingCollection).join(Destination,Destination.id==SortingCollection.destination_id)
        .where(SortingCollection.container_id==container_id,Destination.active==True))
    if not item:return None
    return next(row for row in rows(session,session.get(Operation,item.operation_id)) if row["collection"].id==item.id)
