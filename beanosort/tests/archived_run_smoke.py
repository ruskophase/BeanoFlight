"""Optional actual-archive acceptance in a disposable real-mode TEST database.

No operational database or machine service is opened. Fixture mass records exist
only to exercise the workflow association; they are not real inventory.
"""
import argparse
import json
from pathlib import Path
import secrets
import tempfile

from sqlalchemy import func, select

from beanosort.adapter import export_registry, import_spool
from beanosort.cli import initialize, migrate
from beanosort.db import Database
from beanosort.models import Ledger, Operation, Particle, Station, Version
from beanosort.service import Workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    parser.add_argument("source_run_id")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "var"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="archive-acceptance-", dir=root) as temporary:
        spool = Path(temporary) / "spool"
        manifest = export_registry(args.registry, args.source_run_id, spool)
        url = "sqlite:///" + str(Path(temporary) / "disposable-test.db")
        migrate(url)
        db = Database(url, "real")
        try:
            uid = initialize(db, "archive-test", secrets.token_urlsafe(32))
            wf = Workflow(db)
            with db.read() as s:
                stations = {v.name: v.id for v in s.scalars(select(Station))}
                versions = {v.kind: v.id for v in s.scalars(select(Version))}
            bins = [wf.create_container(uid, label="TEST FIXTURE " + name, location="Disposable test database", capacity="1000", tare_mass="0.5") for name in ("source", "selected", "pass")]
            portion = wf.receive(uid, "archive-test-receipt", {"container_id": bins[0], "station_id": stations["Receiving desk"],
                "net_mass": "1", "ready": True, "notes": "TEST ONLY. No physical stock represented. Database deleted after validation.",
                "evidence": {"kind": "operator_declaration", "method": "Test fixture to validate run association", "tare_basis": "fixture"}})
            op = wf.prepare(uid, "archive-test-prepare", {"portion_id": portion, "planned_mass": "1", "station_id": stations["Manual run record"],
                "selected_id": bins[1], "pass_through_id": bins[2], "ready": True,
                "recipe_id": versions["recipe"], "capture_model_id": versions["capture_model"], "policy_id": versions["reconciliation_policy"]})
            run = wf.start(uid, op, source_run_id=args.source_run_id)
            first = import_spool(wf, uid, run, spool)
            replayed = import_spool(Workflow(db), uid, run, spool)
            assert first["inserted"] == manifest["particle_count"] and replayed["inserted"] == 0
            with db.read() as s:
                profile = wf.profile(s, s.get(Operation, op))
                assert s.scalar(select(func.count()).select_from(Particle)) == manifest["particle_count"]
                assert s.scalar(select(func.count()).select_from(Ledger)) == 1  # ingest posts no stock
                assert profile["counts"]["physical_confirmed"] == 0
                assert profile["missing_source_particles"] == 0
                assert all(p.simulated_destination is None for p in s.scalars(select(Particle)))
            print(json.dumps({"test": "actual archive import and replay", "particles": first["inserted"], "replayed_new_particles": replayed["inserted"],
                "expected": profile["expected"], "physical_confirmed": 0, "operational_inventory_written": False}, indent=2))
        finally:
            db.engine.dispose()


if __name__ == "__main__":
    main()
