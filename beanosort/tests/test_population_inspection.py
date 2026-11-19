from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from beanosort.capture import CAPTURE_DEFAULT
from beanosort.config import Settings
from beanosort.models import Ledger, Operation, Particle, ReferenceDataset, Version
from beanosort.population_inspection import colour_samples, inspect_population
from beanosort.references import create_recipe, import_bundle
from beanosort.web import create_app
from reference_fixture import bundle
from test_references import job, reference
from test_web import client_for


@pytest.mark.parametrize("p", ["0", "0.97", "1"])
def test_clicked_run_bins_match_expected_and_simulated_histograms_without_stock_changes(system, tmp_path, p):
    x = system; did, rid, _ = reference(x, tmp_path); _, op, run = job(x, rid, p=p, count=503)
    while x.workflow.simulate_next(x.uid, run): pass
    client = client_for(x)
    with x.db.read() as s:
        report = x.workflow.profile(s, s.get(Operation, op))["reference_population"]
        before = [(r.id, r.digest, r.simulated_destination) for r in s.scalars(select(Particle).filter_by(run_id=run))]
    edges = report["definitions"]["lab_l"]["edges"]
    buckets = [0, len(edges)//2, len(edges)-2]
    for basis in ("expected", "simulated"):
        for pop in ("input", "selected", "pass_through"):
            source = pop if pop == "input" else basis+"_"+pop
            for bucket in buckets:
                response = client.get(f"/jobs/{op}/populations/inspect", params={"feature":"lab_l", "bucket":bucket, "population":pop, "basis":basis})
                assert response.status_code == 200, response.text
                data = response.json()
                assert data["physical_confirmation"] is False
                assert Decimal(data["matching_count"]) == Decimal(report["populations"][source]["histograms"]["lab_l"][bucket])
                assert len(data["items"]) == len({bean["bean_id"] for bean in data["items"]})
                assert sum(Decimal(bean["count"]) for bean in data["items"]) == Decimal(data["matching_count"])
                assert all(edges[bucket] <= bean["feature_value"] < edges[bucket+1] for bean in data["items"])
                assert all("/reference-datasets/"+did+"/photos/" in bean["photos"]["CamL"] for bean in data["items"])
                if basis == "expected" and pop != "input": assert data["membership_evidence"] == "calculated_prediction"
    with x.db.read() as s:
        assert before == [(r.id, r.digest, r.simulated_destination) for r in s.scalars(select(Particle).filter_by(run_id=run))]
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
    client.close()


def test_preview_pagination_colours_and_pinned_capture_model(system, tmp_path):
    from beanosort.simulation import SimulationConfiguration, current_settings
    x = system; root = bundle(tmp_path/"reference")
    beans = [json.loads(line) for line in (root/"beans.jsonl").read_text().splitlines()]
    for i in range(11, 71):
        beans.append({**beans[0], "bean_id":f"extra/{i}", "bean_sequence":i,
                      "photo_CamL":None, "photo_CamR":None})
    beans[0].update(caml_mean_r_median=63.8, caml_mean_g_median=60.5, caml_mean_b_median=42.2)
    raw="".join(json.dumps(row)+"\n" for row in beans).encode(); (root/"beans.jsonl").write_bytes(raw)
    manifest=json.loads((root/"manifest.json").read_text());manifest["files"][-1]["sha256"]=hashlib.sha256(raw).hexdigest()
    (root/"manifest.json").write_text(json.dumps(manifest))
    did=import_bundle(x.db,x.uid,root,tmp_path/"snapshots",name="Many matching photographed beans")
    rid=create_recipe(x.db,x.uid,"inspect-recipe","Test interval inspection",did,relation="within",sigma="0.5")
    client=client_for(x);page=client.get("/recipes/"+rid)
    endpoint=re.search(r'data-inspection-url="([^"]+)"',page.text).group(1)
    with x.db.read() as s: previous=current_settings(s).id
    SimulationConfiguration(x.db).save(x.uid,"change-preview-capture",dict(previous_id=previous,
        capture_percent="0",mean_mass_g="0.15",material_label="Changed later",reason="Verify open preview pins its model"))
    parsed=urlsplit(endpoint); pinned=dict(parse_qsl(parsed.query))
    pages=[client.get(parsed.path,params={**pinned,"feature":"lab_l","bucket":0,"population":"selected","page":p}).json() for p in (1,2,3)]
    assert all(data["reference_bean_count"]==61 and data["capture_probability"]=="0.99" for data in pages)
    assert [len(data["items"]) for data in pages]==[24,24,13]
    identities=[r["bean_id"] for data in pages for r in data["items"]]
    assert len(set(identities))==61
    assert pages[0]["items"][0]["colours"]=={"CamL":[64,61,42],"CamR":None}
    assert pages[2]["items"][-1]["photos"]=={}
    assert Decimal(pages[0]["matching_count"])==Decimal("60.39")
    assert client.get(parsed.path,params={**pinned,"feature":"lab_l","bucket":1,"population":"input"}).json()["matching_count"]=="0"
    client.close()


def test_inspection_uses_recorded_features_and_excludes_unknowns(system, tmp_path):
    x = system; did, rid, _ = reference(x,tmp_path); _, op, run = job(x,rid,count=100)
    x.workflow.simulate_next(x.uid,run)
    with x.db.read() as s:
        dataset=s.get(ReferenceDataset,did);recipe=s.get(Version,rid).data
        # A missing feature in older source schemas must not be reconstructed
        # into the run's histogram from the newer raw reference record.
        from sqlalchemy import text
        # Only a disposable test fixture is changed, restoring the immutable guard.
        if x.db.engine.dialect.name=="sqlite":
            guard=s.execute(text("SELECT sql FROM sqlite_master WHERE name='particles_no_update'")).scalar_one()
    with x.db.engine.begin() as c:
        if x.db.engine.dialect.name=="sqlite": c.execute(text("DROP TRIGGER particles_no_update"))
        else: c.execute(text("ALTER TABLE particles DISABLE TRIGGER particles_immutable"))
        particles=c.execute(select(Particle.id,Particle.payload).filter_by(run_id=run)).all()
        for key,payload in particles:
            payload["features"].pop("area_mm2",None)
            c.execute(Particle.__table__.update().where(Particle.id==key).values(payload=payload))
        if x.db.engine.dialect.name=="sqlite": c.execute(text(guard))
        else: c.execute(text("ALTER TABLE particles ENABLE TRIGGER particles_immutable"))
    with x.db.read() as s:
        assert inspect_population(s,dataset,recipe,CAPTURE_DEFAULT,feature="area_mm2",bucket=0,run_id=run)["matching_count"]=="0"


@pytest.mark.parametrize("params", [
    {"feature":"unknown","bucket":0}, {"feature":"lab_l","bucket":-1},
    {"feature":"lab_l","bucket":10000}, {"feature":"lab_l","bucket":0,"page":0},
    {"feature":"lab_l","bucket":0,"population":"targeted"},
    {"feature":"lab_l","bucket":0,"basis":"physical"},
    {"feature":"lab_l","bucket":0,"basis":"simulated"},
])
def test_preview_rejects_invalid_or_falsely_observed_inspection(system,tmp_path,params):
    x=system;_,rid,_=reference(x,tmp_path);client=client_for(x)
    assert client.get("/recipes/"+rid+"/inspect",params=params).status_code==409
    client.close()


def test_colour_unknown_and_inspection_requires_authentication(system,tmp_path):
    assert colour_samples({})=={"CamL":None,"CamR":None}
    assert colour_samples({"caml_mean_r_median":False,"caml_mean_g_median":50,"caml_mean_b_median":50})["CamL"] is None
    x=system;_,rid,_=reference(x,tmp_path)
    client=TestClient(create_app(Settings(str(x.db.engine.url),"simulation","secret-for-inspection-"*3)))
    assert client.get("/recipes/"+rid+"/inspect",params={"feature":"lab_l","bucket":0},follow_redirects=False).status_code==303
    client.close()
