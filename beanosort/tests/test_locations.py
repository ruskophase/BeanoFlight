from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.cli import initialize, migrate
from beanosort.db import Database
from beanosort.models import Assignment, Audit, Container, Ledger, Location, Measurement, User, now
from beanosort.quantities import DomainError
from beanosort.service import Workflow, balance
from test_web import client_for, token


def add(x, name):
    return x.workflow.create_location(x.uid, "location-" + uuid4().hex, name)


def move(x, cid, lid, **extra):
    with x.db.read() as s:
        revision = s.get(Container, cid).revision
    return x.workflow.move_container(x.uid, "move-" + uuid4().hex, cid, location_id=lid, revision=extra.get("revision", revision))


def test_locations_are_named_places_with_validation_duplicates_and_retry(system):
    x = system
    key = "new-beans-location"
    lid = x.workflow.create_location(x.uid, key, "Shelf --- NewBeans")
    assert x.workflow.create_location(x.uid, key, "Shelf --- NewBeans") == lid
    with pytest.raises(DomainError, match="already exists"):
        add(x, " shelf --- NEWBEANS ")
    for value in (None, "", " ", "x" * 201, 123):
        with pytest.raises(DomainError):
            add(x, value)
    with x.db.read() as s:
        assert s.get(Location, lid).name == "Shelf --- NewBeans"
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action == "location_created", Audit.data["location_id"].as_string() == lid)) == 1
        assert s.scalar(select(func.count()).select_from(Ledger)) == 0


def test_filled_container_moves_with_contents_and_original_measurements(system):
    x = system
    pid, cid, *_ = x.receive("2", label="Green beans bin")
    lid = add(x, "Shelf --- NewBeans")
    with x.db.read() as s:
        measurements = [(m.id, m.mass_mg, m.provenance) for m in s.scalars(select(Measurement))]
        aid = s.scalar(select(Assignment.id).filter_by(container_id=cid, active=True))
        old_location = s.get(Container, cid).location_id
    for _ in range(2):
        assert x.workflow.move_container(x.uid, "move-retry-test", cid, location_id=lid, revision=1) == cid
    with x.db.read() as s:
        c = s.get(Container, cid)
        assert (c.location_id, c.location, c.revision) == (lid, "Shelf --- NewBeans", 2)
        assert balance(s, pid) == 2000000
        assert s.scalar(select(Assignment.id).filter_by(container_id=cid, active=True)) == aid
        assert [(m.id, m.mass_mg, m.provenance) for m in s.scalars(select(Measurement))] == measurements
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
        audit = s.scalar(select(Audit).filter_by(action="container_moved"))
        assert audit.data["before"]["location_id"] == old_location
        assert audit.data["after"]["location_id"] == lid
        assert audit.data["evidence"] == "simulation" and audit.operator_id == x.uid
    client = client_for(x)
    assert "Green beans bin" in client.get("/locations/" + lid).text
    assert "Green beans bin" not in client.get("/locations/" + old_location).text.split("</tbody>")[0]
    assert "/locations/" + lid in client.get("/portions/" + pid).text
    client.close()


def test_moves_reject_unknown_places_and_jobs_and_keep_archive_rules(system):
    x = system
    pid, cid, *_ = x.receive()
    lid = add(x, "New rack")
    with pytest.raises(DomainError, match="Unknown location"):
        move(x, cid, "location-missing")
    op, data = x.prepare(pid)
    for container in (cid, data["selected_id"]):
        with pytest.raises(DomainError, match="active job"):
            move(x, container, lid)
    x.workflow.transition(x.uid, op, "cancelled", reason="Move after cancelling the boundary")
    move(x, cid, lid)
    dest = data["selected_id"]
    x.workflow.retire_container(x.uid, "archive-empty-dest", dest, revision=1, action="archive")
    with pytest.raises(DomainError, match="Restore"):
        move(x, dest, lid)


def test_concurrent_moves_cannot_overwrite_a_newer_location(system):
    x = system
    _, cid, *_ = x.receive("1")
    lids = [add(x, name) for name in ("Shelf A", "Shelf B")]
    def save(i):
        try:
            return x.workflow.move_container(x.uid, "concurrent-move-" + str(i), cid, location_id=lids[i], revision=1)
        except DomainError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (0, 1)))
    assert results.count("conflict") == 1
    with x.db.read() as s:
        assert s.get(Container, cid).revision == 2
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action == "container_moved")) == 1


def test_location_fk_and_display_name_are_database_enforced(system):
    x = system
    _, cid, *_ = x.receive("1")
    lid = add(x, "Another shelf")
    for query, value in (("location_id", None), ("location_id", lid), ("location", "Unregistered place")):
        with pytest.raises(DBAPIError):
            with x.db.engine.begin() as c:
                c.execute(text(f"UPDATE containers SET {query}=:value WHERE id=:cid"), {"value": value, "cid": cid})
    with x.db.read() as s:
        occupied_location = s.get(Container, cid).location_id
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as c:
            c.execute(text("DELETE FROM locations WHERE id=:id"), {"id": occupied_location})


def test_location_forms_register_pick_and_move_with_csrf(system):
    x = system
    client = client_for(x)
    page = client.get("/locations")
    payload = dict(csrf=token(page), key=token(page,"key"), name="Shelf --- NewBeans")
    assert client.post("/locations", data={**payload,"csrf":"wrong"}).status_code == 403
    page = client.post("/locations", data=payload)
    assert page.status_code == 200
    lid = page.url.path.rsplit("/",1)[-1]
    assert client.post("/locations",data=payload).url.path == page.url.path
    page = client.get("/containers")
    assert 'name="location_id"' in page.text and "Shelf --- NewBeans" in page.text
    page = client.post("/containers",data=dict(csrf=token(page),label="Located vessel",kind="bin",location_id=lid,tare_mass="0.5",capacity="25"))
    assert page.status_code == 200, page.text
    assert page.url.path == "/containers"
    floor = None
    with x.db.read() as s:
        cid = s.scalar(select(Container.id).filter_by(label="Located vessel"))
        floor = s.scalar(select(Location.id).filter_by(name="Floor"))
        s.get(Container,cid)
    bad = client.post(f"/containers/{cid}/move",data=dict(key="missing-csrf-move",location_id=floor,revision=1))
    assert bad.status_code == 403
    page = client.post(f"/containers/{cid}/move",data=dict(csrf=token(page),key="move-to-floor",location_id=floor,revision=1))
    assert page.status_code == 200 and page.url.path == "/handling/move"
    assert not page.url.query and 'Container move recorded' in page.text
    assert 'action="/handling/move" method="post"' not in page.text
    assert "Located vessel" in client.get("/locations/"+floor).text
    assert "Located vessel" not in client.get("/locations/"+lid).text.split("</tbody>")[0]
    client.close()


def test_forward_migration_carries_existing_places_and_allows_legacy_tare_moves(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy-location.db'}"
    config = Config(str(Path(__file__).parents[1]/"alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config,"0004")
    engine = create_engine(url)
    with engine.begin() as c:
        for name in ("Floor","floor","LoadingBay","Shelf --- NewBeans"):
            c.execute(text("INSERT INTO containers (id,created_at,label,kind,location,capacity_mg) VALUES (:id,:at,:id,'bin',:name,25000000)"),
                      {"id":"old-"+name,"at":now(),"name":name})
    engine.dispose()
    migrate(url)
    db = Database(url,"simulation")
    uid = initialize(db,"admin","test-password-123")
    with db.read() as s:
        assert s.scalar(select(func.count()).select_from(Container)) == 4
        assert s.scalar(select(func.count()).select_from(Location)) == 3
        floor = s.get(Container,"old-Floor")
        assert floor.location_id == s.get(Container,"old-floor").location_id
        assert floor.tare_mg is None
        shelf = s.scalar(select(Location.id).filter_by(name="Shelf --- NewBeans"))
    Workflow(db).move_container(uid,"legacy-container-move","old-Floor",location_id=shelf,revision=1)
    with db.read() as s:
        assert s.get(Container,"old-Floor").tare_mg is None
        assert s.get(Container,"old-Floor").location_id == shelf
    db.engine.dispose()
