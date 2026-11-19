from sqlalchemy import select

from beanosort.models import Assignment, Audit, Container, Ledger, Location, Measurement, Operation, Output, Portion, ProductLot, Version
from beanosort.service import balance
from test_web import client_for, token


def test_move_container_from_handling_preserves_filled_contents_and_mass(system):
    x = system
    pid, cid, *_ = x.receive("1.234567", label="Move filled vessel")
    with x.db.read() as s:
        destination = s.scalar(select(Location.id).filter_by(name="LoadingBay"))
        origin = s.get(Container,cid).location_id
        original = s.scalar(select(Assignment.id).filter_by(container_id=cid, active=True))
        measurements = [(m.id, m.mass_mg, m.provenance) for m in s.scalars(select(Measurement))]
    client = client_for(x)
    assert 'href="/handling/move"' in client.get('/handling').text
    page = client.get('/handling/move', params={"container_id": cid})
    assert page.status_code == 200 and '1.234567 kg' in page.text
    data = dict(csrf=token(page),key=token(page,'key'),container_id=cid,location_id=destination,
                revision='1',physical_confirmation='on')
    assert client.post('/handling/move',data={**data,'csrf':'wrong'}).status_code == 403
    assert client.post('/handling/move',data={**data,'physical_confirmation':''}).status_code == 409
    for _ in range(2):
        page = client.post('/handling/move',data=data)
        assert page.status_code == 200 and page.url.path == '/handling/move'
        assert 'container_id' not in page.url.params and 'Container move recorded' in page.text
        assert 'action="/handling/move" method="post"' not in page.text
        assert page.headers['cache-control'] == 'no-store'
    with x.db.read() as s:
        assert s.get(Container,cid).location_id == destination
        assert s.scalar(select(Assignment.id).filter_by(container_id=cid,active=True)) == original
        assert balance(s,pid) == 1234567
        assert [(m.id,m.mass_mg,m.provenance) for m in s.scalars(select(Measurement))] == measurements
        assert len(list(s.scalars(select(Ledger)))) == 1
        assert len(list(s.scalars(select(Audit).filter_by(action='container_moved')))) == 1
    located = client.get('/locations/'+destination)
    assert 'Move filled vessel' in located.text and '1.234567 kg' in located.text
    assert 'class="stock-table"' in located.text and 'href="/portions/'+pid+'"' in located.text
    assert 'Move filled vessel' in client.get('/handling').text
    # A different container can move to the first one's former origin immediately.
    second = x.workflow.create_container(x.uid,label='Next move vessel',location='Floor',capacity='25',tare_mass='0.5')
    page = client.get('/handling/move',params={'container_id':second})
    assert f'<option value="{origin}">' in page.text
    page = client.post('/handling/move',data=dict(csrf=token(page),key=token(page,'key'),container_id=second,
        location_id=origin,revision='1',physical_confirmation='on'))
    assert page.url.path == '/handling/move' and not page.url.query
    # Selecting the first vessel again uses the saved location/revision, allowing return.
    page = client.get('/handling/move',params={'container_id':cid})
    assert f'<option value="{origin}">' in page.text and f'<option value="{destination}">' not in page.text
    assert 'name="revision" type="hidden" value="2"' in page.text
    assert 'name="physical_confirmation" type="checkbox" required' in page.text
    assert token(page,'key') != data['key']
    client.post('/handling/move',data=dict(csrf=token(page),key=token(page,'key'),container_id=cid,
        location_id=origin,revision='2',physical_confirmation='on'))
    with x.db.read() as s:
        assert s.get(Container,cid).location_id == origin and s.get(Container,second).location_id == origin
        assert balance(s,pid) == 1234567 and s.scalar(select(Assignment.id).filter_by(container_id=cid,active=True)) == original
        assert len(list(s.scalars(select(Audit).filter_by(action='container_moved')))) == 3
    client.close()


def test_handling_move_rejects_active_jobs_and_keeps_quality_hold(system):
    x = system
    pid,cid,*_ = x.receive('1')
    with x.db.read() as s:
        destination = s.scalar(select(Location.id).filter_by(name='LoadingBay'))
    op,_ = x.prepare(pid,'1')
    client = client_for(x)
    page = client.get('/handling/move',params={'container_id':cid})
    assert 'part of an active job' in page.text
    assert 'action="/handling/move" method="post"' not in page.text
    assert client.post('/handling/move',data=dict(csrf=token(page),key='blocked-handling-move',container_id=cid,
        location_id=destination,revision=1,physical_confirmation='on')).status_code == 409
    x.workflow.transition(x.uid,op,'cancelled',reason='Move after preparation cancellation')
    x.workflow.set_hold(x.uid,pid,'Quality review needed')
    page = client.get('/handling/move',params={'container_id':cid})
    response = client.post('/handling/move',data=dict(csrf=token(page),key='held-container-move',container_id=cid,
        location_id=destination,revision=1,physical_confirmation='on'))
    assert response.status_code == 200
    with x.db.read() as s:
        assert s.get(Portion,pid).quality == 'held'
        assert s.get(Container,cid).location_id == destination
    client.close()


def test_historical_manual_jobs_specifications_and_product_labels(system):
    x = system
    first, *_ = x.receive("5", label="Web source")
    second, *_ = x.receive("1", label="Web ingredient")
    client = client_for(x)
    evidence = dict(evidence_kind="simulation", method="Browser scale fixture", tare_basis="Net after tare")
    def run(kind, sources, destination_label, execution, **extras):
        destination = x.workflow.create_container(x.uid, label=destination_label, location="Manual bench", capacity="1000", tare_mass="0.5")
        from beanosort.handling import MaterialHandling
        from uuid import uuid4
        old = MaterialHandling(x.db).prepare(x.uid, 'historical-'+uuid4().hex, kind, dict(
                  portion_ids=sources, container_id=destination, station_id=x.stations["Receiving desk"],
                  policy_id=x.versions["reconciliation_policy"], ready=True, **extras))
        page = client.get('/handling/'+old)
        assert page.status_code == 200, page.text
        path = page.url.path
        page = client.post(path + "/start", data={"csrf": token(page)})
        assert page.status_code == 200, page.text
        payload = dict(csrf=token(page), key=token(page, "key"), physical_confirmation="on",
                       representative="on", reason="Training work complete", loss="0", remainder="0", **evidence)
        payload.update(execution)
        page = client.post(path + "/execute", data=payload)
        assert page.status_code == 200, page.text
        if kind != "transfer":
            page = client.post(path + "/release", data=dict(csrf=token(page), accept_estimates="on", reason="Training release"))
            assert page.status_code == 200, page.text
        with x.db.read() as s:
            current = s.scalar(select(Assignment).filter_by(container_id=destination, active=True)).portion_id
        return current, page
    moved, page = run("transfer", [first], "Moved vessel", {})
    assert moved == first
    withdrawn, page = run("split", [moved], "Withdrawn vessel", {"output_mass": "2", "remainder": "3"}, withdrawn_mass="2")
    old_label = client.get("/scan", params={"code": moved})
    assert "Historical contents label" in old_label.text
    sample = client.get("/portions/" + withdrawn)
    page = client.post("/portions/" + withdrawn + "/count-sample", data=dict(csrf=token(sample), key=token(sample, "key"),
                station_id=x.stations["Receiving desk"], sample_count="100", sample_mass="0.1", sample_returned="on", representative="on", **evidence))
    assert page.status_code == 200, page.text
    blend, page = run("blend", [withdrawn, second], "Blend vessel", {"output_mass": "3", "actual_" + withdrawn: "2", "actual_" + second: "1"})
    assert "calculated prediction" in page.text
    page = client.get("/specifications")
    page = client.post("/specifications", data=dict(csrf=token(page), kind="packaging_specification", name="Web kg", sku="WEB-BEAN", nominal_mass="1"))
    assert page.status_code == 200, page.text
    with x.db.read() as s:
        spec = s.scalar(select(Version.id).filter_by(name="Web kg"))
    packed, page = run("packaging", [blend], "Product crate", {"pack_masses": "0.99\n1.01", "remainder": "1"}, specification_id=spec)
    assert "Inspect product lot" in page.text
    with x.db.read() as s:
        lot = s.scalar(select(ProductLot).filter_by(portion_id=packed))
    product = client.get("/products/" + lot.id)
    assert product.status_code == 200 and "0.99 kg" in product.text and "WEB-BEAN" in product.text
    assert client.get("/scan", params={"code": lot.id}).url.path == "/products/" + lot.id
    label = client.get("/labels/product/" + lot.id)
    assert "https://beans.example.test/products/" + lot.id in label.text
    assert client.get("/qr/product/" + lot.id).headers["content-type"] == "image/svg+xml"
    next_plan = client.get("/handling/new?kind=packaging").text
    assert 'name="portion_ids" value="' + first + '"' not in next_plan
    assert 'name="portion_ids" value="' + packed + '"' not in next_plan
    assert client.get("/handling").status_code == 200


def test_supervised_correction_and_hold_disposition_forms(system):
    x = system
    pid, *_ = x.receive()
    op, _ = x.prepare(pid)
    x.start(op, 5)
    x.finish(op)
    x.reconcile(op, selected="15.5")
    client = client_for(x)
    page = client.get("/jobs/" + op)
    page = client.post("/jobs/" + op + "/correct", data=dict(csrf=token(page), key=token(page, "key"),
                selected="16", pass_through="4", remainder="0", retained="0", loss="0", sample="0", reason="Reviewed transcription",
                evidence_kind="simulation", method="Corrected scale fixture", tare_basis="Net after tare"))
    assert page.status_code == 200, page.text
    assert "Reviewed transcription" in page.text
    with x.db.read() as s:
        current = s.scalar(select(Assignment).where(Assignment.active == True, Assignment.portion_id != pid)).portion_id
    page = client.get("/portions/" + current)
    page = client.post("/portions/" + current + "/hold", data=dict(csrf=token(page), reason="Label inspection"))
    page = client.post("/portions/" + current + "/clear-hold", data=dict(csrf=token(page), reason="Label verified"))
    assert page.status_code == 200 and "quality hold cleared" in page.text
    assert client.get("/operations/" + op).url.path == "/jobs/" + op
