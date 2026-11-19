from pathlib import Path
import re
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.cli import initialize, migrate
from beanosort.db import Database
from beanosort.models import Audit, Container, Ledger, Station, identity, now
from beanosort.quantities import DomainError
from beanosort.service import Workflow, balance
from test_web import client_for, token


def test_new_container_requires_both_positive_precise_weights(system):
    x = system
    base = dict(label="Required weights", location="Bench", capacity="25", tare_mass="0.512345")
    for field in ("capacity", "tare_mass"):
        for value in (None, "", " ", "0", "-1", "NaN", "Infinity", "0.0000001", 0.5):
            with pytest.raises(DomainError):
                x.workflow.create_container(x.uid, **{**base, field: value})
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Container)) == 0
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action == "container_created")) == 0
    cid = x.workflow.create_container(x.uid, **base)
    with x.db.read() as s:
        c = s.get(Container, cid)
        assert c.capacity_mg == 25000000
        assert c.tare_mg == 512345
        registration = s.scalar(select(Audit).where(Audit.action == "container_created"))
        assert registration.data["capacity_mg"] == 25000000
        assert registration.data["tare_mg"] == 512345
        assert registration.data["tare_evidence"] == "simulation"
        assert registration.operator_id == x.uid
        assert s.scalar(select(func.count()).select_from(Ledger)) == 0


def test_capacity_is_net_material_and_tare_does_not_change_stock(factory):
    x = factory("real")
    # A heavy vessel can have a smaller permitted material load than its tare.
    cid = x.workflow.create_container(x.uid, label="Heavy vessel", location="Bench", capacity="1", tare_mass="2.5")
    data = dict(container_id=cid, station_id=x.stations["Receiving desk"], ready=True,
                net_mass="1.200001", evidence=x.evidence)
    with pytest.raises(DomainError, match="capacity"):
        x.workflow.receive(x.uid, "above-container-capacity", data)
    pid = x.workflow.receive(x.uid, "exact-container-capacity", {**data, "net_mass": "1.2"})
    with x.db.read() as s:
        assert balance(s, pid) == 1200000
        assert s.get(Container, cid).capacity_mg == 1000000
        registration = s.scalar(select(Audit).where(Audit.action == "container_created"))
        assert registration.data["tare_evidence"] == "operator_declaration"


def test_database_enforces_new_container_weights_and_cannot_clear_them(system):
    x = system
    for capacity, tare in ((None, 100), (100, None), (None, None), (0, 100), (100, 0), (100, -1)):
        with pytest.raises(DBAPIError):
            with x.db.engine.begin() as c:
                c.execute(text("INSERT INTO containers (id,created_at,label,kind,location,capacity_mg,tare_mg) "
                               "VALUES (:id,:at,:label,'bin','Bench',:capacity,:tare)"),
                          dict(id=identity("bin"), at=now(), label=uuid4().hex, capacity=capacity, tare=tare))
    cid = x.workflow.create_container(x.uid, label="Cannot clear", location="Bench", capacity="2", tare_mass="0.1")
    for field in ("capacity_mg", "tare_mg"):
        for value in (None, 0, -1):
            with pytest.raises(DBAPIError):
                with x.db.engine.begin() as c:
                    c.execute(text(f"UPDATE containers SET {field}=:value WHERE id=:id"), {"value": value, "id": cid})


def test_container_form_requires_weights_and_displays_them(system):
    client = client_for(system)
    page = client.get("/containers")
    for field in ("capacity", "tare_mass"):
        assert "required" in re.search(r'<input name="' + field + r'"[^>]*>', page.text)[0]
    data = dict(csrf=token(page), label="Form tare", location="Shelf", kind="bin", capacity="25", tare_mass="0.512345")
    for field in ("capacity", "tare_mass"):
        assert client.post("/containers", data={k: v for k, v in data.items() if k != field}).status_code == 409
        assert client.post("/containers", data={**data, field: ""}).status_code == 409
    response = client.post("/containers", data=data)
    assert response.status_code == 200
    assert response.url.path == "/containers"
    with system.db.read() as s:
        cid = s.scalar(select(Container.id).filter_by(label=data["label"]))
    assert f'href="/containers/{cid}"' in response.text
    response = client.get("/containers/" + cid)
    assert "0.512345 kg" in response.text
    assert "25 kg net material" in response.text
    assert "Tare evidence: simulation" in response.text
    client.close()


def test_upgrade_preserves_unknown_legacy_weights(tmp_path):
    """Exercise a forward upgrade with actual pre-0005 records and no backfill."""
    url = f"sqlite:///{tmp_path / 'legacy-containers.db'}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "0004")
    engine = create_engine(url)
    with engine.begin() as c:
        for label, capacity in (("Legacy unknown", None), ("Legacy capacity", 25000000)):
            c.execute(text("INSERT INTO containers (id,created_at,label,kind,location,capacity_mg) "
                           "VALUES (:id,:at,:label,'bin','Old shelf',:capacity)"),
                      dict(id=label, at=now(), label=label, capacity=capacity))
    engine.dispose()
    migrate(url)
    db = Database(url, "simulation")
    uid = initialize(db, "admin", "test-password-123")
    with db.read() as s:
        assert s.get(Container, "Legacy unknown").capacity_mg is None
        assert s.get(Container, "Legacy unknown").tare_mg is None
        assert s.get(Container, "Legacy capacity").capacity_mg == 25000000
        assert s.get(Container, "Legacy capacity").tare_mg is None
        station = s.scalar(select(Station.id).filter_by(name="Receiving desk"))
    # Legacy records remain usable without fabricating either weight.
    Workflow(db).receive(uid, "legacy-container-receipt", dict(container_id="Legacy unknown", station_id=station,
        net_mass="1", ready=True, evidence={"kind": "simulation", "method": "fixture", "tare_basis": "net entered"}))
    with pytest.raises(DBAPIError):
        with db.engine.begin() as c:
            c.execute(text("INSERT INTO containers (id,created_at,label,kind,location) "
                           "VALUES ('new',:at,'New invalid','bin','Shelf')"), {"at": now()})
    db.engine.dispose()
