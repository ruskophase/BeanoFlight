"""Explicit recipe modes; no machine-control connection."""
from sqlalchemy import select

from .models import Destination, Version

HOPPER_RECIPE_NAME = "Empty hopper · all beans to Pass-through"


def is_hopper_recipe(data):
    return is_machine_recipe(data) or (data.get("schema") == "beanosort-reference-recipe/v1"
            and data.get("operation_mode") == "hopper_emptying" and data.get("relation") == "pass_all")


def is_machine_recipe(data):
    return data.get("schema") == "beanosort-machine-recipe/v1" and data.get("operation_mode") == "all_pass_through"


def hopper_mode(session, operation):
    return is_hopper_recipe(session.get(Version, operation.recipe_id).data)


def source_return(session, operation):
    return any(d.readiness.get("source_return") is True for d in
               session.scalars(select(Destination).where(Destination.operation_id == operation.id)))
