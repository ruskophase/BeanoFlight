from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.auth import hash_password
from beanosort.capture import fixture_particle
from beanosort.cli import migrate, seed_demo
from beanosort.db import Database
from beanosort.models import (Assignment, Audit, Container, Ledger, Lineage, Measurement, Operation, Output,
                             Particle, Portion, Profile, Receipt, Reconciliation, Reservation, Run, User, Version, identity)
from beanosort.quantities import DomainError, kg, mass
from beanosort.service import Workflow, available, balance, trace


@pytest.mark.parametrize("value,unit,expected", [("1.234567", "kg", 1234567), ("0.001", "g", 1), ("17", "mg", 17)])
def test_quantity_precision(value, unit, expected):
    assert mass(value, unit) == expected
    assert mass(kg(expected)) == expected


@pytest.mark.parametrize("value,unit", [(1.2, "kg"), ("0.0000001", "kg"), ("-1", "kg"), ("NaN", "kg"), ("1", "litre"), (True, "mg")])
def test_invalid_quantities(value, unit):
    with pytest.raises(DomainError):
        mass(value, unit)


def test_receipt_trace_precision_and_retry(system):
    x = system
    pid, cid, key, data = x.receive("1.234567")
    assert x.workflow.receive(x.uid, key, data) == pid
    with x.db.read() as s:
        portion = s.get(Portion, pid)
        receipt = s.get(Receipt, portion.receipt_id)
        assert receipt.supplier == "Supplier"
        assert receipt.source_lot_id != pid
        assert balance(s, pid) == 1234567
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
        assert trace(s, pid)[0][0].receipt_id == receipt.id
    with pytest.raises(DomainError, match="different content"):
        x.workflow.receive(x.uid, key, {**data, "net_mass": "2"})
    with pytest.raises(DomainError, match="already has contents"):
        x.workflow.receive(x.uid, "new-receipt-key", data)


def test_assignments_capacity_and_readiness(system):
    x = system
    pid, cid, _, data = x.receive()
    _, prepare = x.prepare(pid)
    with pytest.raises(DomainError):
        x.workflow.prepare(x.uid, "another-prepare", {**prepare, "selected_id": cid})
    empty = x.workflow.create_container(x.uid, label="Small", location="Shelf", capacity="1", tare_mass="0.5")
    with pytest.raises(DomainError, match="capacity"):
        x.workflow.receive(x.uid, "too-much-mass", {**data, "container_id": empty})
    with pytest.raises(DomainError, match="physically"):
        x.workflow.receive(x.uid, "not-confirmed", {**data, "container_id": empty, "ready": False})


def test_competing_reservations_across_connections(system):
    x = system
    pid, *_ = x.receive()
    first, data = x.prepare(pid)
    x.workflow.transition(x.uid, first, "cancelled", reason="Set up concurrent users")
    other = Workflow(Database(x.db.engine.url, "simulation"))
    with x.db.write() as s:
        uid2 = identity("user")
        s.add(User(id=uid2, username="second", role="operator", password_hash=hash_password("second-password")))
    def reserve(pair):
        wf, uid = pair
        try:
            return wf.prepare(uid, "compete-" + uuid4().hex, data)
        except DomainError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [(x.workflow, x.uid), (other, uid2)]))
    other.db.engine.dispose()
    assert results.count("conflict") == 1
    with x.db.read() as s:
        assert available(s, s.get(Portion, pid)) == 0
        assert s.scalar(select(func.count()).select_from(Reservation).where(Reservation.active == True)) == 1


def test_full_flow_restart_replay_frozen_profiles(system):
    x = system
    opid = seed_demo(x.db, x.uid)
    assert seed_demo(x.db, x.uid) == opid
    fresh = Workflow(Database(x.db.engine.url, "simulation"))
    with x.db.read() as s:
        run = s.scalar(select(Run).filter_by(operation_id=opid))
        before_ledger = s.scalar(select(func.count()).select_from(Ledger))
        before_particles = s.scalar(select(func.count()).select_from(Particle))
        before_profiles = s.scalar(select(func.count()).select_from(Profile))
        outputs = list(s.scalars(select(Output).filter_by(operation_id=opid)))
        assert {o.mass_mg for o in outputs} == {4000000, 16000000}
        for output in outputs:
            assert available(s, s.get(Portion, output.portion_id)) == output.mass_mg
            related, edges = trace(s, output.portion_id)
            assert any(p.receipt_id for p in related)
            assert edges[0].contribution == "ancestry_known_proportions_unknown"
        profile = s.scalar(select(Profile).filter_by(operation_id=opid, stream="run")).data
        assert profile["expected"]["selected"] == "194.00"
        assert profile["composition_evidence"] == "calculated_prediction"
        assert profile["synthetic_monitor"]["evidence"] == "simulation"
        assert profile["counts"]["physical_confirmed"] == 0
    assert fresh.simulate_next(x.uid, run.id, replay_first=True) == 0
    fresh.release(x.uid, opid, accept_estimates=True, reason="retry")
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Ledger)) == before_ledger
        assert s.scalar(select(func.count()).select_from(Particle)) == before_particles
        assert s.scalar(select(func.count()).select_from(Profile)) == before_profiles
    fresh.db.engine.dispose()


def test_interrupted_processing_and_explicit_remainder(system):
    x = system
    pid, cid, *_ = x.receive()
    op, _ = x.prepare(pid, "10")
    run = x.start(op)
    x.workflow.simulate_next(x.uid, run)
    x.workflow.transition(x.uid, op, "failed", reason="Fixture feeder interruption")
    with x.db.read() as s:
        assert available(s, s.get(Portion, pid)) == 0
        assert balance(s, pid) == 20000000
    x.finish(op)
    rid, key, data = x.reconcile(op, selected="6", pass_through="3", remainder="10", retained="0.5", loss="0.3", sample="0.2")
    assert x.workflow.reconcile(x.uid, key, op, data) == rid
    with x.db.read() as s:
        outputs = list(s.scalars(select(Output).filter_by(operation_id=op)))
        assert len(outputs) == 4
        assert balance(s, pid) == 0
        assert all(available(s, s.get(Portion, o.portion_id)) == 0 for o in outputs)
        assert s.scalar(select(Reconciliation).filter_by(operation_id=op)).discrepancy_mg == 0
        assert s.scalar(select(Profile).filter_by(operation_id=op, stream="run")).data["missing_source_particles"] == 750
        assert sum(o.mass_mg for o in outputs) + 500000 == 20000000
        remainder = next(o for o in outputs if o.stream == "remainder")
        assert s.scalar(select(Assignment).filter_by(container_id=cid, active=True)).portion_id == remainder.portion_id
        assert s.scalar(select(Profile).filter_by(portion_id=remainder.portion_id)).data["count"] is None
    x.workflow.release(x.uid, op, accept_estimates=True, reason="Measured partial run accounted; limited coverage accepted")
    with x.db.read() as s:
        retained = next(o for o in outputs if o.stream == "retained")
        assert available(s, s.get(Portion, retained.portion_id)) == 0
        assert s.get(Portion, retained.portion_id).status == "wip"
        assert s.get(Portion, pid).status == "consumed"


def test_missing_measurements_and_discrepancy_stay_held(system):
    x = system
    pid, *_ = x.receive()
    op, _ = x.prepare(pid)
    x.start(op)
    x.finish(op)
    with pytest.raises(DomainError, match="Not reconciled"):
        x.reconcile(op, remainder="")
    x.reconcile(op, selected="15.9")
    with pytest.raises(DomainError, match="exceeds"):
        x.workflow.release(x.uid, op, accept_estimates=True, reason="Attempt release")
    with x.db.read() as s:
        recon = s.scalar(select(Reconciliation).filter_by(operation_id=op))
        assert recon.discrepancy_mg == 100000
        assert recon.accounting["loss_mg"] == 0
        assert s.get(Operation, op).release_status == "held"


def test_duplicate_ingestion_out_of_order_and_conflicting_retry(system):
    x = system
    pid, *_ = x.receive()
    op, _ = x.prepare(pid)
    run = x.start(op, 10)
    row = fixture_particle(5)
    assert x.workflow.ingest(x.uid, run, "later", [row]) == 1
    assert x.workflow.ingest(x.uid, run, "later", [row]) == 0
    assert x.workflow.ingest(x.uid, run, "retry-new-batch", [row]) == 0
    with x.db.read() as s:
        assert x.workflow.profile(s, s.get(Operation, op))["source_sequence_gaps"] == 5
    x.workflow.ingest(x.uid, run, "earlier", [fixture_particle(i) for i in range(5)])
    with pytest.raises(DomainError, match="different data"):
        x.workflow.ingest(x.uid, run, "later", [fixture_particle(0)])
    conflict = deepcopy(row)
    conflict["features"]["brightness"] = 0
    with pytest.raises(DomainError, match="different data"):
        x.workflow.ingest(x.uid, run, "conflicting-particle", [conflict])
    bad = fixture_particle(9)
    bad["schema"] = "invalid"
    with pytest.raises(DomainError):
        x.workflow.ingest(x.uid, run, "atomic-malformed", [fixture_particle(8), bad])
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Particle)) == 6
    x.finish(op)
    with pytest.raises(DomainError, match="late"):
        x.workflow.ingest(x.uid, run, "late", [fixture_particle(8)])


def test_real_inventory_and_synthetic_monitor_are_not_conflated(factory):
    x = factory("real")
    pid, *_ = x.receive()
    op, _ = x.prepare(pid)
    run = x.start(op)
    with pytest.raises(ValueError, match="isolated"):
        seed_demo(x.db, x.uid)
    with pytest.raises(DomainError):
        x.workflow.simulate_next(x.uid, run)
    with pytest.raises(DomainError, match="cannot cross"):
        x.workflow.ingest(x.uid, run, "synthetic", [fixture_particle(0)])
    real = fixture_particle(0)
    real["evidence"] = {"features": {"kind": "image_derived", "source": "test-real-input-contract", "version": "v1"},
                        "decision": {"kind": "model_derived", "source": "test-classifier", "version": "v1"},
                        "action": {"kind": "controller_record", "source": "test-controller", "version": "v1"}}
    x.workflow.ingest(x.uid, run, "reviewed-real-contract", [real])
    with x.db.read() as s:
        profile = x.workflow.profile(s, s.get(Operation, op))
        assert profile["expected"]["selected"] == "0.97"
        assert profile["counts"]["physical_confirmed"] == 0
        assert profile["synthetic_monitor"]["counts"]["selected"] == 0
        assert s.scalar(select(Particle)).simulated_destination is None
    other = Database(x.db.engine.url, "simulation")
    with pytest.raises(RuntimeError, match="environment"):
        with other.read():
            pass
    other.engine.dispose()


def test_database_guards_and_immutable_versions(system):
    x = system
    pid, *_ = x.receive()
    opid, data = x.prepare(pid)
    for sql in (
        "UPDATE versions SET version = 99",
        "DELETE FROM ledger",
        "UPDATE operations SET boundary_mg = boundary_mg + 1",
        "UPDATE world SET environment = 'real'",
    ):
        with pytest.raises(DBAPIError):
            with x.db.engine.begin() as c:
                c.execute(text(sql))
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            op = s.get(Operation, opid)
            s.add(Ledger(id=identity("movement"), operation_id=opid, portion_id=pid, delta_mg=-20000001,
                         reason="invalid debit", operator_id=x.uid, station_id=op.station_id))
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            s.add(Reservation(id=identity("r"), portion_id=pid, operation_id=opid, mass_mg=20000001))
    with x.db.read() as s:
        assert balance(s, pid) == 20000000


def test_profile_immutability_and_acyclic_lineage(system):
    x = system
    opid = seed_demo(x.db, x.uid)
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as c:
            c.execute(text("UPDATE profiles SET version = 2"))
    with x.db.read() as s:
        edge = s.scalar(select(Lineage))
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            s.add(Lineage(parent_id=edge.child_id, child_id=edge.parent_id, operation_id=opid))


def test_hold_after_reservation_blocks_start(system):
    x = system
    pid, *_ = x.receive()
    op, _ = x.prepare(pid)
    x.workflow.set_hold(x.uid, pid, "Inspect contamination")
    with pytest.raises(DomainError, match="held"):
        x.start(op)
    with x.db.read() as s:
        assert s.get(Operation, op).status == "prepared"


def test_policies_versioned_and_explicit_release_permission(system):
    x = system
    pid, *_ = x.receive()
    op, data = x.prepare(pid)
    x.workflow.create_version(x.uid, "reconciliation_policy", "Exact balance with explicit composition approval",
                              {"tolerance_mg": 100000, "allow_estimated_composition": True})
    x.start(op)
    x.finish(op)
    x.reconcile(op)
    with pytest.raises(DomainError, match="Explicit approval"):
        x.workflow.release(x.uid, op, accept_estimates=False, reason="no")
    with x.db.write() as s:
        uid = identity("user")
        s.add(User(id=uid, username="operator", role="operator", password_hash=hash_password("operator-password")))
    with pytest.raises(DomainError, match="requires"):
        x.workflow.release(uid, op, accept_estimates=True, reason="not permitted")
    with x.db.read() as s:
        assert s.get(Operation, op).policy_id == data["policy_id"]
        assert s.get(Version, data["policy_id"]).data["tolerance_mg"] == 0


def test_migrations_repeat_without_rewriting_stock(system):
    x = system
    pid, *_ = x.receive()
    migrate(x.db.engine.url.render_as_string(hide_password=False))
    with x.db.read() as s:
        assert balance(s, pid) == 20000000


def test_pause_with_new_quality_hold_cannot_resume(system):
    x = system
    pid, *_ = x.receive()
    op, _ = x.prepare(pid)
    x.start(op)
    x.workflow.transition(x.uid, op, "paused", reason="Inspect material")
    x.workflow.set_hold(x.uid, pid, "Contamination suspected")
    with pytest.raises(DomainError, match="cannot resume"):
        x.workflow.transition(x.uid, op, "running")


def test_policy_requires_integer_milligrams(system):
    for tolerance in ("10", 1.2, True):
        with pytest.raises(DomainError, match="integer"):
            system.workflow.create_version(system.uid, "reconciliation_policy", "Invalid",
                                           {"tolerance_mg": tolerance, "allow_estimated_composition": True})


def test_destinations_distinct_and_double_completion_blocked(system):
    x = system
    pid, cid, *_ = x.receive()
    first, data = x.prepare(pid)
    x.workflow.transition(x.uid, first, "cancelled", reason="Test invalid replacements")
    with pytest.raises(DomainError, match="different containers"):
        x.workflow.prepare(x.uid, "same-destinations", {**data, "pass_through_id": data["selected_id"]})
    with pytest.raises(DomainError, match="different containers"):
        x.workflow.prepare(x.uid, "destination-is-source", {**data, "selected_id": cid})
    op, _ = x.prepare(pid)
    x.start(op)
    x.finish(op)
    _, _, reconciliation = x.reconcile(op)
    with pytest.raises(DomainError, match="once"):
        x.workflow.reconcile(x.uid, "new-completion-key", op, reconciliation)
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Reconciliation).where(Reconciliation.operation_id == op)) == 1
