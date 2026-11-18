from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.models import Assignment, Audit, Container, Ledger, Measurement, User
from beanosort.quantities import DomainError
from beanosort.service import balance
from test_handling import execute, prepare
from test_web import client_for, token


def create(x, label="Editable"):
    return x.workflow.create_container(x.uid, label=label, location="Shelf", capacity="25", tare_mass="0.5")


def edit_data(x, cid, **extra):
    with x.db.read() as s:
        c = s.get(Container, cid)
        data = dict(label=c.label, location=c.location, kind=c.kind, capacity="25", tare_mass="0.6", revision=c.revision)
    return {**data, **extra}


def update(x, cid, **extra):
    return x.workflow.update_container(x.uid, "edit-" + uuid4().hex, cid, edit_data(x, cid, **extra))


def retire(x, cid, action, **extra):
    with x.db.read() as s:
        revision = s.get(Container, cid).revision
    return x.workflow.retire_container(x.uid, "remove-" + uuid4().hex, cid, revision=revision, action=action, **extra)


def test_editing_preserves_material_identity_and_past_measurements(system):
    x = system
    pid, cid, *_ = x.receive("2", label="Old name")
    with x.db.read() as s:
        measurements = [(m.id, m.mass_mg, m.provenance) for m in s.scalars(select(Measurement))]
        assignment_id = s.scalar(select(Assignment.id).filter_by(container_id=cid, active=True))
    data = edit_data(x, cid, label="New name", location="Shelf C", kind="tote", capacity="2", tare_mass="1.234567")
    key = "edit-retry-test"
    assert x.workflow.update_container(x.uid, key, cid, data) == cid
    assert x.workflow.update_container(x.uid, key, cid, data) == cid
    with x.db.read() as s:
        c = s.get(Container, cid)
        assert (c.label, c.location, c.kind, c.capacity_mg, c.tare_mg, c.revision) == ("New name", "Shelf C", "tote", 2000000, 1234567, 2)
        assert balance(s, pid) == 2000000
        assert [(m.id, m.mass_mg, m.provenance) for m in s.scalars(select(Measurement))] == measurements
        assert s.scalar(select(Assignment.id).filter_by(container_id=cid, active=True)) == assignment_id
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
        changes = list(s.scalars(select(Audit).filter_by(action="container_updated")))
        assert len(changes) == 1
        assert changes[0].data["before"]["label"] == "Old name"
        assert changes[0].data["after"]["tare_mg"] == 1234567
        assert changes[0].data["tare_evidence"] == "simulation"
    page = client_for(x).get("/containers/" + cid)
    assert "1.234567 kg" in page.text and "entered during container edit" in page.text


def test_validation_and_lower_capacity_cannot_reduce_stock(system):
    x = system
    pid, cid, *_ = x.receive("2")
    other = create(x, "Taken name")
    for changes in ({"label": "Taken name"}, {"label": " "}, {"kind": ""}, {"location": ""},
                    {"capacity": "1.999999"}, {"capacity": ""}, {"tare_mass": ""},
                    {"tare_mass": "0"}, {"capacity": "-1"}, {"tare_mass": 0.5}, {"tare_mass": "NaN"}):
        with pytest.raises(DomainError):
            update(x, cid, **changes)
    with x.db.read() as s:
        assert s.get(Container, cid).revision == 1
        assert balance(s, pid) == 2000000
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as c:
            c.execute(text("UPDATE containers SET capacity_mg=1999999 WHERE id=:id"), {"id": cid})


def test_prepared_source_and_destinations_locked_then_cancelled_job_keeps_history(system):
    x = system
    pid, cid, *_ = x.receive()
    op, data = x.prepare(pid)
    for container in (cid, data["selected_id"], data["pass_through_id"]):
        with pytest.raises(DomainError, match="active job"):
            update(x, container)
        for action in ("delete", "archive"):
            with pytest.raises(DomainError, match="contents or is in an active job"):
                retire(x, container, action, confirmed=True)
    x.workflow.transition(x.uid, op, "cancelled", reason="Cancelled training boundary")
    update(x, cid, label="Source after cancellation")
    retire(x, data["selected_id"], "archive")
    retire(x, data["selected_id"], "delete", confirmed=True)
    with x.db.read() as s:
        assert s.get(Container, data["selected_id"]).deleted is True


def test_concurrent_edits_fail_stale_session_and_retry_is_safe(system):
    x = system
    cid = create(x)
    data = edit_data(x, cid)
    def save(i):
        try:
            return x.workflow.update_container(x.uid, "concurrent-edit-" + str(i), cid, {**data, "label": "Edited " + str(i)})
        except DomainError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (0, 1)))
    assert results.count("conflict") == 1
    with pytest.raises(DomainError, match="another session"):
        x.workflow.update_container(x.uid, "stale-update", cid, data)
    with pytest.raises(DomainError, match="another session"):
        x.workflow.retire_container(x.uid, "stale-delete", cid, action="delete", revision=1, confirmed=True)


def test_used_container_can_archive_restore_and_keep_old_labels(system):
    x = system
    pid, cid, *_ = x.receive("1", label="History vessel")
    for action in ("archive", "delete"):
        with pytest.raises(DomainError, match="contents"):
            retire(x, cid, action, confirmed=True)
    op, handler, _ = prepare(x, "transfer", [pid])
    execute(x, handler, op)
    retire(x, cid, "archive")
    client = client_for(x)
    assert "History vessel" not in client.get("/containers").text
    assert "History vessel" in client.get("/containers?include_archived=true").text
    for path in ("/receive", "/jobs/new", "/handling/new?kind=transfer"):
        assert 'value="' + cid + '"' not in client.get(path).text
    scanned = client.get("/scan", params={"code": cid})
    assert "Archived container" in scanned.text and pid in scanned.text
    with pytest.raises(DomainError, match="archived"):
        x.workflow.receive(x.uid, "archived-receipt", dict(container_id=cid, station_id=x.stations["Receiving desk"], net_mass="1", ready=True, evidence=x.evidence))
    with pytest.raises(DomainError, match="Restore"):
        update(x, cid)
    retire(x, cid, "restore")
    assert "History vessel" in client.get("/containers").text
    update(x, cid, tare_mass="0.7")
    retire(x, cid, "delete", confirmed=True)
    scanned = client.get("/scan", params={"code": cid})
    assert scanned.status_code == 410 and pid in scanned.text
    with x.db.read() as s:
        container = s.get(Container, cid)
        assert container.deleted is True and container.location_id is None
        assert balance(s, pid) == 1000000
    client.close()


def test_delete_unused_requires_confirmation_and_preserves_scan_tombstone(system):
    x = system
    cid = create(x, "Deleted name")
    with pytest.raises(DomainError, match="Confirm deletion"):
        retire(x, cid, "delete")
    key = "delete-safe-retry"
    for _ in range(2):
        assert x.workflow.retire_container(x.uid, key, cid, revision=1, action="delete", confirmed=True) == cid
    replacement = create(x, "Deleted name")
    assert replacement != cid
    with x.db.read() as s:
        assert s.get(Container, cid) is None
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action == "container_deleted")) == 1
    client = client_for(x)
    old = client.get("/scan", params={"code": "http://old-server/containers/" + cid})
    assert old.status_code == 410 and "Deleted container" in old.text
    assert cid in old.text and replacement not in old.text
    client.close()


def test_database_rejects_archived_assignments_and_occupied_archive(system):
    x = system
    pid, cid, *_ = x.receive("1")
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as c:
            c.execute(text("UPDATE containers SET archived=true WHERE id=:id"), {"id": cid})
    archived = create(x, "Archived")
    retire(x, archived, "archive")
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as c:
            c.execute(text("UPDATE assignments SET container_id=:cid WHERE portion_id=:pid AND active"), {"cid": archived, "pid": pid})


def test_edit_and_removal_forms_require_auth_csrf_and_removal_role(system):
    x = system
    cid = create(x)
    client = client_for(x)
    page = client.get("/containers/" + cid + "/edit")
    data = dict(csrf=token(page), key=token(page,"key"), **edit_data(x,cid,label="Form edited"))
    assert client.post(f"/containers/{cid}/edit", data={**data, "csrf":"invalid"}).status_code == 403
    response = client.post(f"/containers/{cid}/edit", data=data)
    assert response.status_code == 200 and "Form edited" in response.text
    assert f'/containers/{cid}/delete' not in client.get('/containers').text
    confirmation = client.get(f'/containers/{cid}/delete')
    assert confirmation.status_code == 200 and 'Delete container' in confirmation.text
    assert f'action="/containers/{cid}/delete"' in confirmation.text
    with x.db.read() as s:
        assert s.get(Container, cid) is not None  # Opening the page is read-only.
    with x.db.write() as s:
        s.get(User,x.uid).role = "operator"
    denied = client.post(f"/containers/{cid}/delete", data=dict(csrf=token(response),key="operator-delete",revision=2,confirmed="on"))
    assert denied.status_code == 409 and "supervisor/admin" in denied.text
    assert client.get(f'/containers/{cid}/delete').status_code == 409
    assert client.get(f"/containers/{cid}/edit").status_code == 200
    client.close()
