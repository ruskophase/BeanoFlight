import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from beanosort.cli import seed_demo
from beanosort.config import Settings
from beanosort.models import Assignment, Container, Input, Operation, Output, Portion, Receipt, Run
from beanosort.web import create_app


def token(response, name="csrf"):
    return re.search(r'name="' + name + r'" value="([^"]+)"', response.text).group(1)


def client_for(system):
    settings = Settings(system.db.engine.url.render_as_string(hide_password=False), system.db.environment, "test-secret-" * 4,
                        public_url="https://beans.example.test")
    client = TestClient(create_app(settings))
    page = client.get("/login")
    response = client.post("/login", data={"csrf": token(page), "username": "admin", "password": "test-password-123"})
    assert response.status_code == 200
    return client


def test_operator_end_to_end_using_html_forms(system):
    x = system
    client = client_for(x)
    containers = []
    for label in ("UI raw", "UI selected", "UI pass"):
        page = client.get("/containers")
        response = client.post("/containers", data={"csrf": token(page), "label": label, "location": "UI shelf", "kind": "tote", "capacity": "30", "tare_mass": "0.5"})
        assert response.status_code == 200
        assert response.url.path == "/containers"
        with x.db.read() as s:
            containers.append(s.scalar(select(Container.id).filter_by(label=label)))
    page = client.get("/receive")
    with x.db.read() as s:
        location_id = s.get(Container, containers[0]).location_id
    receive_data = {"csrf": token(page), "key": token(page, "key"), "receiving_load_id": "0", "receiving_container_id": containers[0],
                    "receiving_container_revision": "1", "receiving_location_id": location_id,
                    "station_id": x.stations["Receiving desk"], "receiving_net_mass": "20.000", "supplier": "UI supplier",
                    "received_date": "2026-11-10", "ready": "on", "tared": "0", "placed": "on"}
    response = client.post("/receive", data=receive_data)
    assert response.status_code == 200, response.text
    receipt_id = response.url.path.split("/")[-1]
    with x.db.read() as s:
        pid = s.scalar(select(Output.portion_id).filter_by(operation_id=s.get(Receipt, receipt_id).operation_id))
    assert client.post("/receive", data=receive_data).url.path == "/receipts/" + receipt_id
    page = client.get("/jobs/new")
    prepared = client.post("/jobs", data={"csrf": token(page), "key": token(page, "key"), "portion_id": pid,
        "planned_mass": "20", "station_id": x.stations["Training sorter"], "selected_id": containers[1],
        "pass_through_id": containers[2], "ready": "on", "recipe_id": x.versions["recipe"],
        "capture_model_id": x.versions["capture_model"], "policy_id": x.versions["reconciliation_policy"]})
    assert prepared.status_code == 200, prepared.text
    op_path = prepared.url.path
    page = client.post(op_path + "/start", data={"csrf": token(prepared), "seed": "UI-seed", "count": "250"})
    assert page.status_code == 200
    page = client.post(op_path + "/simulate", data={"csrf": token(page)})
    assert "250 particle records received" in page.text
    assert "48.50" in page.text
    for target in ("finishing", "awaiting_reconciliation"):
        page = client.post(op_path + "/transition", data={"csrf": token(page), "target": target, "drained": "on"})
        assert page.status_code == 200, page.text
    data = {"csrf": token(page), "key": token(page, "key"), "selected": "3.5", "pass_through": "16.5",
            "remainder": "0", "retained": "0", "loss": "0", "sample": "0", "reason": "Entered independent training weights",
            "evidence_kind": "simulation", "method": "Scale fixture", "tare_basis": "Tared empty bins"}
    page = client.post(op_path + "/reconcile", data=data)
    assert page.status_code == 200, page.text
    assert "Reconciliation recorded" in page.text
    page = client.post(op_path + "/release", data={"csrf": token(page), "accept_estimates": "on", "reason": "Training release"})
    assert page.status_code == 200
    assert "Completed" in page.text
    container = client.get("/containers/" + containers[1])
    assert "3.5 kg" in container.text
    assert "calculated prediction" in container.text
    with x.db.read() as s:
        output = s.scalar(select(Assignment).filter_by(container_id=containers[1], active=True)).portion_id
    detail = client.get("/portions/" + output)
    assert "UI supplier" in detail.text
    assert "simulation" in detail.text
    label = client.get("/labels/portion/" + output)
    assert "https://beans.example.test/portions/" + output in label.text
    assert client.get("/qr/portion/" + output).headers["content-type"] == "image/svg+xml"
    replay = client.post(op_path + "/replay", data={"csrf": token(page)})
    assert replay.status_code == 200
    old_label = client.get("/scan", params={"code": pid})
    assert "Historical contents label" in old_label.text
    assert "Manual sorter controls" in page.text
    client.close()


def test_stale_label_does_not_resolve_to_new_contents(system):
    x = system
    seed_demo(x.db, x.uid)
    client = client_for(x)
    with x.db.read() as s:
        raw_id = s.scalar(select(Container.id).filter_by(label="Demo raw"))
        old = s.scalar(select(Assignment).filter_by(container_id=raw_id, active=False)).portion_id
    new = x.workflow.receive(x.uid, "reuse-container", dict(container_id=raw_id, station_id=x.stations["Receiving desk"],
                                                          net_mass="2", ready=True, evidence=x.evidence))
    scanned = client.get("/scan", params={"code": "https://old-address.test/portions/" + old})
    assert scanned.url.path == "/portions/" + old
    assert "Historical contents label" in scanned.text
    current = client.get("/containers/" + raw_id)
    assert new in current.text
    assert "2 kg" in current.text


def test_auth_csrf_stale_station_and_evidence_guide(system):
    settings = Settings(system.db.engine.url.render_as_string(hide_password=False), "simulation", "secret-for-test-" * 3)
    client = TestClient(create_app(settings))
    assert client.post("/login", data={"csrf": "missing", "username": "admin", "password": "test-password-123"}).status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/containers", data={"label": "no auth"}, follow_redirects=False).status_code == 303
    client = client_for(system)
    assert client.post("/containers", data={"label": "CSRF"}).status_code == 403
    assert "Stale / no connection confirmed" in client.get("/").text
    assert "1,940" in client.get("/evidence").text
    assert client.get("/health").json()["hardware_control"] is True
    assert client.get("/health").json()["automatic_job_control"] is False
    assert client.get("/static/app.css").status_code == 200


def test_no_sorter_or_hardware_imports():
    import ast
    from pathlib import Path
    forbidden = {"beanoflight", "beanofastcap", "serial", "zmq", "RPi", "gpiozero", "cv2"}
    for path in (Path(__file__).parents[1] / "src" / "beanosort").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(n.name.split(".")[0] not in forbidden for n in node.names)
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden
