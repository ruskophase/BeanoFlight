import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.adapter import bundle_features, export_registry, import_spool, sha256
from beanosort.capture import CAPTURE_DEFAULT, digest, expected_profile
from beanosort.models import Operation, Particle, SourceBundle
from beanosort.quantities import DomainError
from beanosort.service import Workflow


RUN = "0123456789abcdef0123456789abcdef"


def archive(tmp_path, count=6):
    """Required fields from inspected BeanRegistry schema 3; no sorter import."""
    db = tmp_path / "registry.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript('''
        PRAGMA user_version=3;
        CREATE TABLE sessions(run_id TEXT PRIMARY KEY,status TEXT,source_kind TEXT,settings_json TEXT,created_timestamp_ns INTEGER,updated_timestamp_ns INTEGER);
        CREATE TABLE beans(run_id TEXT,sequence INTEGER,revision INTEGER,status TEXT,created_timestamp_ns INTEGER,updated_timestamp_ns INTEGER,PRIMARY KEY(run_id,sequence));
        CREATE TABLE sorting_decisions(decision_id TEXT PRIMARY KEY,run_id TEXT,sequence INTEGER,registry_revision INTEGER,source TEXT,timestamp_ns INTEGER,actuation_timestamp_ns INTEGER,gate_indices_json TEXT,policy_version TEXT,reason TEXT);
        CREATE TABLE actuation_results(decision_id TEXT PRIMARY KEY,run_id TEXT,sequence INTEGER,registry_revision INTEGER,source TEXT,actual_open_timestamp_ns INTEGER,actual_close_timestamp_ns INTEGER,success INTEGER,detail TEXT);
        CREATE TABLE enrichments(result_id TEXT PRIMARY KEY,run_id TEXT,sequence INTEGER,source TEXT,kind TEXT,value_json TEXT,timestamp_ns INTEGER,model_version TEXT,confidence REAL);
        CREATE TABLE registry_events(event_sequence INTEGER PRIMARY KEY,run_id TEXT);
        ''')
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (RUN, "completed", "live-shared-rg10-green", '{"classifier":"test-v1"}', 100, 300))
        for i in range(count):
            c.execute("INSERT INTO beans VALUES(?,?,?,?,?,?)", (RUN, i + 1, i + 1, "exited", 100 + i, 200 + i))
        for i, gates, reason in [(1, "[1,2]", "target"), (2, "[3]", "target"), (3, "[]", "category accepted; fixture"),
                                  (4, "[]", "classification arrived too late"), (5, "[]", "no gate reached the probability threshold; fixture")]:
            c.execute("INSERT INTO sorting_decisions VALUES(?,?,?,?,?,?,?,?,?,?)", (f"d{i}", RUN, i, i, "beano-sorter", 100, 150, gates, "policy-v1", reason))
        for i, source in [(1, "esp32-s2-gptimer"), (2, "virtual-actuator-deadline")]:
            c.execute("INSERT INTO actuation_results VALUES(?,?,?,?,?,?,?,?,?)", (f"d{i}", RUN, i, i, source, 150, 155, 1, "gate cycle recorded"))
        c.execute("INSERT INTO enrichments VALUES(?,?,?,?,?,?,?,?,?)", ("e1", RUN, 1, "classification-worker", "classification_decision_basis", '{"category":"reject","confidence":0.8}', 101, "model-v1", 0.8))
        c.execute("INSERT INTO registry_events VALUES(?,?)", (999, RUN))
    return db


def test_offline_contract_preserves_unknown_virtual_commands_and_no_physical_confirmation(tmp_path):
    db = archive(tmp_path)
    before = sha256(db)
    spool = tmp_path / "spool"
    manifest = export_registry(db, RUN, spool)
    assert sha256(db) == before
    assert manifest["registry_cursor"] == 999
    rows = json.loads((spool / manifest["batches"][0]["path"]).read_text())
    assert rows[0]["action"] == "attempted" and rows[0]["source"]["gate_cycle"]["source"] == "esp32-s2-gptimer"
    assert rows[1]["action"] == "unknown" and rows[1]["evidence"]["action"]["kind"] == "simulation"
    assert rows[2]["decision"] == "pass"
    assert rows[3]["decision"] == "unknown"
    assert rows[4]["decision"] == "selected" and rows[4]["action"] == "not_issued"
    assert rows[5]["decision"] == "unknown"
    assert rows[0]["key"] == RUN + "/1" and rows[0]["source_at"] is None
    assert rows[0]["evidence"]["classification"]["confidence_is_probability"] is False
    profile = expected_profile([{"payload": r} for r in rows], CAPTURE_DEFAULT)
    assert profile["expected"] == {"selected": "0.97", "pass_through": "1.03", "unresolved": "4"}
    assert profile["counts"]["physical_confirmed"] == 0
    assert profile["synthetic_monitor"]["counts"]["selected"] == 0
    assert profile["histograms"]["selected"]["classification_category"]["reject"] == "0.97"


def test_restart_partial_import_and_replay_without_duplicate_evidence(factory, tmp_path):
    db = archive(tmp_path, 602)
    spool = tmp_path / "spool"
    manifest = export_registry(db, RUN, spool)
    x = factory("real")
    pid, *_ = x.receive("2")
    op, _ = x.prepare(pid, "2")
    run = x.workflow.start(x.uid, op, source_run_id=RUN)
    original = x.workflow.ingest
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 2:
            raise OSError("Simulated process termination after committed batch")
        return original(*args, **kwargs)
    x.workflow.ingest = interrupted
    with pytest.raises(OSError):
        import_spool(x.workflow, x.uid, run, spool)
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Particle)) == 500
        assert x.workflow.profile(s, s.get(Operation, op))["missing_source_particles"] == 102
    x.workflow = Workflow(x.db)
    assert import_spool(x.workflow, x.uid, run, spool)["inserted"] == 102
    assert import_spool(x.workflow, x.uid, run, spool)["inserted"] == 0
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Particle)) == 602
        assert s.scalar(select(func.count()).select_from(SourceBundle)) == 1
        profile = x.workflow.profile(s, s.get(Operation, op))
        assert profile["missing_source_particles"] == 0 and "physical coverage unknown" in profile["coverage"]
        assert profile["comparability_key"] is None
        assert all(p.simulated_destination is None for p in s.scalars(select(Particle)))
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            s.execute(text("DELETE FROM source_bundles"))
    x.finish(op)
    x.reconcile(op, selected="1", pass_through="1")
    x.workflow.release(x.uid, op, accept_estimates=True, reason="Test record only; incomplete physical coverage accepted")
    assert import_spool(x.workflow, x.uid, run, spool)["inserted"] == 0
    manifest["settings"]["classifier"] = "changed"
    manifest["fingerprint"] = digest({k: v for k, v in manifest.items() if k != "fingerprint"})
    (spool / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DomainError, match="snapshot"):
        import_spool(x.workflow, x.uid, run, spool)


def test_spool_validation_and_inventory_isolation(system, tmp_path):
    db = archive(tmp_path)
    spool = tmp_path / "spool"
    manifest = export_registry(db, RUN, spool)
    pid, *_ = system.receive()
    op, _ = system.prepare(pid)
    run = system.start(op)
    with pytest.raises(DomainError, match="environment/source"):
        import_spool(system.workflow, system.uid, run, spool)
    batch = spool / manifest["batches"][0]["path"]
    batch.write_text("[]")
    with pytest.raises(DomainError, match="hash"):
        import_spool(system.workflow, system.uid, run, spool)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE sessions SET status='running'")
    with pytest.raises(DomainError, match="archived"):
        export_registry(db, RUN, tmp_path / "active")


def test_statistics_exact_identity_and_file_provenance(tmp_path):
    db = archive(tmp_path)
    directory = tmp_path / "statistics"
    directory.mkdir()
    (directory / "beans.jsonl").write_text(json.dumps({"bean_sequence": 1, "sample_count": 3, "caml_lab_l_mean_median": 72.5,
          "caml_lab_chroma_mean_median": 4.2, "caml_ellipse_major_mm_median": 9.1, "rotational_ellipsoid_volume_proxy_mm3_median": 182}) + "\n")
    manifest = {"schema": "beanoflight-live-statistics-bundle/v1", "summary": {"source_run_id": RUN[:8]},
                "files": [{"path": "beans.jsonl", "sha256": sha256(directory / "beans.jsonl")}], "provenance": {}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DomainError, match="exact matching"):
        bundle_features(directory, RUN)
    manifest["summary"]["source_run_id"] = RUN
    (directory / "manifest.json").write_text(json.dumps(manifest))
    exported = export_registry(db, RUN, tmp_path / "spool", statistics=directory)
    row = json.loads((tmp_path / "spool" / exported["batches"][0]["path"]).read_text())[0]
    assert row["features"]["lab_l"] == 72.5 and row["features"]["volume_proxy_mm3"] == 182
    assert row["features"]["shape"] is None and row["features"]["defects"] is None
    assert row["source"]["statistics_sample_count"] == 3
    assert row["source"]["calibration_comparability"] == "unknown"
    assert exported["statistics"]["beans_sha256"] == manifest["files"][0]["sha256"]

    # The live derived bundle uses approximate camera medians and pixel geometry.
    # Never invent a combined_*_median field or convert px³ into calibrated mm³.
    (directory / "beans.jsonl").write_text(json.dumps({"bean_sequence": 1, "caml_approx_lab_l_median": 62.0,
        "caml_approx_lab_chroma_median": 7.1, "rotational_ellipsoid_volume_proxy_px3_median": 12000,
        "derived_observation_count": 3, "sensor_edge_observation_count": 1}) + "\n")
    manifest["files"][0]["sha256"] = sha256(directory / "beans.jsonl")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    exported = export_registry(db, RUN, tmp_path / "live-spool", statistics=directory)
    row = json.loads((tmp_path / "live-spool" / exported["batches"][0]["path"]).read_text())[0]
    assert row["features"]["lab_l"] == 62 and row["features"]["volume_proxy_mm3"] is None
    assert row["source"]["feature_mapping"]["lab_l"] == "caml_approx_lab_l_median"
    assert row["source"]["statistics_coverage"]["sensor_edge_observation_count"] == 1


def test_mock_model_members_are_not_promoted_to_real_classification(tmp_path):
    db = archive(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO enrichments VALUES(?,?,?,?,?,?,?,?,?)", ("mock-member", RUN, 1, "beano-inference", "classification_evidence",
                    "{}", 101, "mock-stereo-resnet18-fp16-v1", 0.99))
        c.execute("UPDATE enrichments SET model_version=?,value_json=? WHERE result_id='e1'", ("mean-probability-v1", json.dumps({
            "category": "insect_damage", "ensemble": {"member_result_ids": ["mock-member"]}})))
    spool = tmp_path / "spool"
    manifest = export_registry(db, RUN, spool)
    row = json.loads((spool / manifest["batches"][0]["path"]).read_text())[0]
    assert row["features"]["classification_category"] is None
    assert row["evidence"]["classification"]["kind"] == "simulation"
    assert row["decision"] == "selected" and row["action"] == "attempted"
    assert manifest["source_versions"]["classification_model_versions"] == ["mock-stereo-resnet18-fp16-v1"]
    assert manifest["source_versions"]["classification_pool_versions"] == ["mean-probability-v1"]
