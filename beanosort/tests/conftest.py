import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from beanosort.cli import initialize, migrate
from beanosort.db import Database
from beanosort.models import Station, Version
from beanosort.service import Workflow


@pytest.fixture
def factory(tmp_path):
    databases, schemas = [], []

    def make(environment="simulation"):
        postgres = os.environ.get("BEANOSORT_TEST_POSTGRES_URL")
        if postgres:
            url = make_url(postgres)
            if not url.database.endswith("_test"):
                raise RuntimeError("PostgreSQL tests require a dedicated database ending in _test")
            schema = "test_" + uuid4().hex
            engine = create_engine(url)
            with engine.begin() as c:
                c.execute(text(f'CREATE SCHEMA "{schema}"'))
            schemas.append((engine, schema))
            url = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        else:
            url = f"sqlite:///{tmp_path / (uuid4().hex + '.db')}"
        migrate(url)
        db = Database(url, environment)
        uid = initialize(db, "admin", "test-password-123")
        workflow = Workflow(db)
        with db.read() as s:
            stations = {x.name: x.id for x in s.scalars(select(Station))}
            # Regression fixtures retain the original capture contract. New
            # Simulation Settings workflows are exercised separately.
            versions = {x.kind: x.id for x in s.scalars(select(Version).where(Version.name != "Simulation capture"))}
        evidence = {"kind": "simulation" if environment == "simulation" else "direct_measurement",
                    "method": "test scale", "tare_basis": "net after tare", "precision_mg": 1}
        bundle = SimpleNamespace(db=db, workflow=workflow, uid=uid, stations=stations, versions=versions, evidence=evidence)

        def receive(amount="20", label=None, **extra):
            cid = workflow.create_container(uid, label=label or "Bin " + uuid4().hex, location="Test shelf", capacity="1000", tare_mass="0.5")
            data = dict(container_id=cid, station_id=stations["Receiving desk"], net_mass=amount, ready=True,
                        supplier="Supplier", origin="Origin", supplier_lot="BAG-01", bean_type="Pulse", evidence=evidence)
            data.update(extra)
            key = "receive-" + uuid4().hex
            return workflow.receive(uid, key, data), cid, key, data

        def prepare(portion_id, amount="20"):
            destinations = [workflow.create_container(uid, label="Destination " + uuid4().hex, location="Test floor", capacity="1000", tare_mass="0.5") for _ in range(2)]
            data = dict(portion_id=portion_id, planned_mass=amount,
                        station_id=stations["Training sorter" if environment == "simulation" else "Manual run record"],
                        selected_id=destinations[0], pass_through_id=destinations[1], ready=True,
                        recipe_id=versions["recipe"], capture_model_id=versions["capture_model"], policy_id=versions["reconciliation_policy"])
            return workflow.prepare(uid, "prepare-" + uuid4().hex, data), data

        def start(op, count=1000):
            return workflow.start(uid, op, seed="test-seed", count=count, source_run_id="external-" + op)

        def finish(op):
            workflow.transition(uid, op, "finishing")
            workflow.transition(uid, op, "awaiting_reconciliation", drained=True)

        def reconcile(op, **quantities):
            data = dict(selected="16", pass_through="4", remainder="0", retained="0", loss="0", sample="0",
                        reason="Explicit test accounting", evidence=evidence)
            data.update(quantities)
            key = "reconcile-" + uuid4().hex
            return workflow.reconcile(uid, key, op, data), key, data

        bundle.receive, bundle.prepare, bundle.start, bundle.finish, bundle.reconcile = receive, prepare, start, finish, reconcile
        databases.append(db)
        return bundle

    yield make
    for db in databases:
        db.engine.dispose()
    for engine, schema in schemas:
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def system(factory):
    return factory()
