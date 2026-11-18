"""A distinct weighing row for every collection, with one source remainder."""
from copy import deepcopy
from decimal import Decimal

from .models import Container, Destination
from .quantities import DomainError
from .segments import collections,current,latest_weight


def collection_rows(session,operation,source):
    rows=[];alias=False
    for collection in collections(session,operation):
        destination=session.get(Destination,collection.destination_id)
        shared=bool(destination.readiness.get("source_return"));alias |= shared
        rows.append({"stream":collection.code,"logical_stream":collection.stream,"collection_id":collection.id,
            "collection":collection,"shared_source":shared,"saved_weight":latest_weight(session,collection),
            "title":("Source / Pass-through" if shared else collection.stream.replace("_"," ").title())+f" · collection {collection.ordinal}",
            "container":session.get(Container,collection.container_id)})
    if not alias:
        rows.append({"stream":"remainder","logical_stream":"remainder","collection_id":None,"shared_source":False,
            "saved_weight":None,"title":"Source container — beans remaining","container":session.get(Container,source.container_id)})
    return rows


def collection_context(session,operation,environment,rows,model):
    from .collections import baselines,starting_mass
    from .weighing import kg_entry,predicted_mass
    from .sorting_modes import hopper_mode
    prior=baselines(session,operation);whole=deepcopy(model["populations"])
    model["collections"]={};model["segmented"]=True
    for row in rows:
        code=row["stream"]
        if row.get("collection"):
            item=predicted_mass(session,operation,environment,collection=row["collection"])
            pop=deepcopy(item["populations"][row["logical_stream"]])
            model["collections"][code]=item
            if row["shared_source"]:
                remainder=whole["remainder"]
                if pop["predicted_net_mg"] is not None and remainder["predicted_net_mg"] is not None:
                    pop["predicted_net_mg"]+=remainder["predicted_net_mg"]
                    count=Decimal(pop["expected_count"])+Decimal(remainder["expected_count"])
                    pop.update(expected_count=str(count),mean_bean_mass_mg=str(Decimal(pop["predicted_net_mg"])/count) if count else remainder["mean_bean_mass_mg"])
            model["populations"][code]=pop
        else:pop=whole["remainder"]
        baseline=prior.get(code,{}).get("existing_mass_mg",0)
        predicted=pop["predicted_net_mg"]
        row.update(starting_mass_mg=baseline,predicted_added_mg=predicted,
                   predicted_net_mg=predicted+baseline if predicted is not None else None)
        row["gross_value"]=kg_entry(row["predicted_net_mg"]+row["container"].tare_mg) if row["predicted_net_mg"] is not None and row["container"].tare_mg is not None else ""
        saved=row["saved_weight"]
        if saved and saved.tare_mg==row["container"].tare_mg:
            row["gross_value"]=kg_entry(saved.gross_mg)
    return {"rows":rows,"model":model,"input_total_mg":operation.boundary_mg+starting_mass(session,operation),
            "segmented":True,"segment_id":current(session,operation).id,"hopper_emptying":hopper_mode(session,operation)}
