from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.handling import (MaterialHandling, effective_outputs, effective_reconciliation, evaluate_specification,
                               latest_profile, predict_blend, validate_specification)
from beanosort.models import (Assignment, Audit, Correction, HandlingPlan, Input, Ledger, Lineage, Measurement,
                             Operation, Output, Pack, Portion, ProductLot, Profile, Reconciliation)
from beanosort.quantities import DomainError
from beanosort.service import available, balance, trace
from beanosort.cli import seed_material_demo


def prepare(x, kind, portions, **extra):
    destination = x.workflow.create_container(x.uid, label="Manual " + uuid4().hex, location="Manual bench", capacity="1000", tare_mass="0.5")
    data = dict(portion_ids=portions, container_id=destination, station_id=x.stations["Receiving desk"],
                policy_id=x.versions["reconciliation_policy"], ready=True, **extra)
    handler = MaterialHandling(x.db)
    op = handler.prepare(x.uid, "plan-" + uuid4().hex, kind, data)
    return op, handler, data


def execute(x, handler, op, **extra):
    handler.start(x.uid, op)
    data = dict(reason="Recorded manual fixture work", physical_confirmation=True, evidence=x.evidence,
                loss="0", remainder="0", representative=True)
    data.update(extra)
    key = "execute-" + uuid4().hex
    assert handler.execute(x.uid, key, op, data) == op
    assert handler.execute(x.uid, key, op, data) == op
    return data


def release(x, op):
    x.workflow.release(x.uid, op, accept_estimates=True, reason="Reviewed fixture quantities and evidence")


def outputs(x, op):
    with x.db.read() as s:
        return {o.stream: o.portion_id for o in effective_outputs(s, s.get(Operation, op))}


def test_receipt_sort_split_blend_package_genealogy(system):
    x = system
    raw, *_ = x.receive("20")
    sort, _ = x.prepare(raw)
    run = x.start(sort, 250)
    x.workflow.simulate_next(x.uid, run)
    x.finish(sort)
    x.reconcile(sort)
    release(x, sort)
    sorted_ids = outputs(x, sort)
    h = MaterialHandling(x.db)
    profile_id = h.count_sample(x.uid, "count-sort-sample", sorted_ids["selected"], 100, "0.1",
                               {"station_id": x.stations["Receiving desk"], "evidence": x.evidence, "representative": True, "sample_returned": True})
    split, h, _ = prepare(x, "split", [sorted_ids["selected"]], withdrawn_mass="5")
    execute(x, h, split, output_mass="5", remainder="11")
    with x.db.read() as s:
        children = effective_outputs(s, s.get(Operation, split))
        assert sum(o.mass_mg for o in children) == 16000000
        assert all(available(s, s.get(Portion, o.portion_id)) == 0 for o in children)
        descriptions = [latest_profile(s, o.portion_id).data for o in children]
        assert sum(Decimal(d["estimated_count"]) for d in descriptions) == Decimal(16000)
        assert all(d["composition_evidence"] == "inherited_estimate" and "counts" not in d for d in descriptions)
        assert {d["dependence_group"] for d in descriptions} == {split}
        assert {d["source_profile_id"] for d in descriptions} == {profile_id}
    release(x, split)
    split_ids = outputs(x, split)
    second, *_ = x.receive("3", supplier="Another supplier")
    blend, h, _ = prepare(x, "blend", [split_ids["withdrawn"], second])
    execute(x, h, blend, output_mass="8", actual_inputs={split_ids["withdrawn"]: "5", second: "3"}, reason="Tumbled together in clean vessel")
    release(x, blend)
    blend_id = outputs(x, blend)["blend"]
    spec = x.workflow.create_version(x.uid, "packaging_specification", "1 kg product", {"sku": "DEMO-BEAN", "nominal_mg": 1000000})
    pack, h, _ = prepare(x, "packaging", [blend_id], specification_id=spec)
    execute(x, h, pack, pack_masses=["0.999", "1.001", "1", "1", "1", "1"], remainder="2")
    release(x, pack)
    with x.db.read() as s:
        lot = s.scalar(select(ProductLot).filter_by(operation_id=pack))
        records = list(s.scalars(select(Pack).filter_by(lot_id=lot.id).order_by(Pack.ordinal)))
        assert len(records) == 6 and records[0].actual_net_mg == 999000
        assert lot.nominal_mg == 1000000 and lot.specification_id == spec
        ancestors, edges = trace(s, lot.portion_id)
        assert {p.id for p in ancestors if p.receipt_id} == {raw, second}
        assert {sort, split, blend, pack} <= {e.operation_id for e in edges}
        assert all(e.contribution == "ancestry_known_proportions_unknown" for e in edges)
        assert sum(balance(s, p.id) for p in s.scalars(select(Portion))) == 23000000
        assert sum(available(s, p) for p in s.scalars(select(Portion))) == 23000000
        assert latest_profile(s, lot.portion_id).data["moisture"]["evidence"] == "unknown"
        assert latest_profile(s, blend_id).data["distributions"] == {}  # second input has no count/feature evidence
    with pytest.raises(DomainError, match="Opened-pack"):
        prepare(x, "split", [lot.portion_id], withdrawn_mass="1")
    with pytest.raises(DomainError, match="Opened-pack"):
        x.prepare(lot.portion_id, "1")


def test_transfer_preserves_identity_and_can_return_to_reused_bin(system):
    x = system
    portion, original, *_ = x.receive("2")
    op, h, plan = prepare(x, "transfer", [portion])
    execute(x, h, op)
    with x.db.read() as s:
        assert s.scalar(select(Assignment).filter_by(portion_id=portion, active=True)).container_id == plan["container_id"]
        assert s.scalar(select(func.count()).select_from(Lineage)) == 0
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
        assert balance(s, portion) == available(s, s.get(Portion, portion)) == 2000000
    returned = h.prepare(x.uid, "return-original-bin", "transfer", {**plan, "container_id": original})
    execute(x, h, returned)
    with x.db.read() as s:
        assert s.scalar(select(Assignment).filter_by(container_id=original, active=True)).portion_id == portion
        assert s.scalar(select(func.count()).select_from(Lineage)) == 0
        assert s.scalar(select(func.count()).select_from(Assignment)) == 3


def test_manual_reservations_interruption_discrepancy_and_loss(system):
    x = system
    pid, *_ = x.receive("5")
    op, h, plan = prepare(x, "split", [pid], withdrawn_mass="2")
    with pytest.raises(DomainError, match="unreserved"):
        h.prepare(x.uid, "second-conflicting-plan", "split", plan)
    h.start(x.uid, op)
    x.workflow.transition(x.uid, op, "failed", reason="Fixture interruption")
    with x.db.read() as s:
        assert available(s, s.get(Portion, pid)) == 0
    data = dict(output_mass="2", remainder="2.8", loss="0.1", reason="Measured outputs with unexplained difference",
                physical_confirmation=True, representative=False, evidence=x.evidence)
    h.execute(x.uid, "interrupted-split-account", op, data)
    with x.db.read() as s:
        recon = effective_reconciliation(s, s.get(Operation, op))
        assert recon.discrepancy_mg == 100000
        assert recon.accounting["loss_mg"] == 100000
        assert balance(s, pid) == 0
        assert sum(balance(s, p.id) for p in s.scalars(select(Portion))) == 4800000
        assert all(latest_profile(s, o.portion_id).data["estimated_count"] is None for o in effective_outputs(s, s.get(Operation, op)))
    with pytest.raises(DomainError, match="exceeds"):
        release(x, op)
    with pytest.raises(DomainError, match="only once"):
        h.execute(x.uid, "repeat-different-command", op, data)


def test_count_weighted_blend_and_unknown_specification():
    ingredients = [dict(portion_id="a", mass_mg=1000000, profile_id="pa", profile={"comparability_key": "cal-v1", "count_per_kg": "100", "distributions": {"shape": {"round": "1"}}}),
                   dict(portion_id="b", mass_mg=1000000, profile_id="pb", profile={"comparability_key": "cal-v1", "count_per_kg": "200", "distributions": {"shape": {"round": "0"}}})]
    predicted = predict_blend(ingredients)
    assert Decimal(predicted["distributions"]["shape"]["round"]) == Decimal(1) / 3
    assert predicted["estimated_count"] == "300" and predicted["count_per_kg"] == "150"
    spec = {"constraints": [{"feature": "shape", "category": "round", "basis": "count", "unit": "fraction", "minimum": "0.3", "maximum": "0.4"}]}
    validate_specification("product_specification", spec)
    evaluation = evaluate_specification(predicted, spec)
    assert evaluation["status"] == "prediction_pass" and not evaluation["verified_compliance"]
    strict = deepcopy(spec)
    strict["constraints"][0]["required_evidence"] = ["direct_measurement"]
    assert evaluate_specification(predicted, strict)["status"] == "unknown"
    for change in ({"count_per_kg": None}, {"comparability_key": None}):
        missing = deepcopy(ingredients)
        missing[0]["profile"].update(change)
        assert evaluate_specification(predict_blend(missing), spec)["status"] == "unknown"
    unknown = deepcopy(predicted)
    unknown["distributions"]["shape"]["unknown"] = "0.01"
    assert evaluate_specification(unknown, spec)["status"] == "unknown"
    with pytest.raises(DomainError):
        validate_specification("product_specification", {"constraints": [{**spec["constraints"][0], "minimum": "not-a-number"}]})


def test_corrections_supersede_held_outputs_and_preserve_history(system):
    x = system
    source, *_ = x.receive()
    op, _ = x.prepare(source)
    x.start(op, 10)
    x.finish(op)
    x.reconcile(op, selected="15.9")
    old = outputs(x, op)
    h = MaterialHandling(x.db)
    data = dict(selected="16", pass_through="4", remainder="0", retained="0", loss="0", sample="0",
                evidence=x.evidence, reason="Reviewed scale transcription correction")
    correction = h.correct_sorting(x.uid, "correction-key-v1", op, data)
    assert h.correct_sorting(x.uid, "correction-key-v1", op, data) == correction
    new = outputs(x, op)
    assert not set(old.values()) & set(new.values())
    with x.db.read() as s:
        assert s.scalar(select(Reconciliation).filter_by(operation_id=op)).discrepancy_mg == 100000
        assert effective_reconciliation(s, s.get(Operation, op)).discrepancy_mg == 0
        assert all(s.get(Portion, p).status == "consumed" and balance(s, p) == 0 for p in old.values())
        assert all(available(s, s.get(Portion, p)) == 0 for p in new.values())
        assert any(p.id == source for p in trace(s, new["selected"])[0])
        assert s.scalar(select(Measurement).filter_by(operation_id=op, role="selected")).mass_mg == 15900000
    x.workflow.set_hold(x.uid, new["selected"], "Inspect corrected label")
    h.clear_hold(x.uid, new["selected"], "Label checked")
    with x.db.read() as s:
        assert available(s, s.get(Portion, new["selected"])) == 0
    release(x, op)
    with x.db.read() as s:
        assert available(s, s.get(Portion, new["selected"])) == 16000000
        from beanosort.operation_labels import describe_operation
        assert describe_operation(s, op)['final_masses_mg'] == {'Source':0, 'Selected':16000000, 'Pass-through':4000000}
    with pytest.raises(DomainError, match="unreleased"):
        h.correct_sorting(x.uid, "correction-after-release", op, data)
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            s.execute(text("UPDATE corrections SET reason='overwrite'"))


def test_required_spec_evidence_blocks_release_and_definitions_immutable(system):
    x = system
    a, *_ = x.receive("1")
    b, *_ = x.receive("1")
    spec = x.workflow.create_version(x.uid, "product_specification", "Measured shape", {"constraints": [
        {"feature": "shape", "category": "round", "basis": "count", "unit": "fraction", "required_evidence": ["direct_measurement"]}]})
    op, h, _ = prepare(x, "blend", [a, b], specification_id=spec)
    execute(x, h, op, output_mass="2", actual_inputs={a: "1", b: "1"})
    with pytest.raises(DomainError, match="evidence"):
        release(x, op)
    with pytest.raises(DBAPIError):
        with x.db.write() as s:
            s.execute(text("UPDATE handling_plans SET specification_id=NULL"))


def test_pack_precision_and_cancel_restore_availability(system):
    x = system
    pid, *_ = x.receive("2")
    spec = x.workflow.create_version(x.uid, "packaging_specification", "kg", {"sku": "TEST", "nominal_mg": 1000000})
    op, h, _ = prepare(x, "packaging", [pid], specification_id=spec)
    x.workflow.transition(x.uid, op, "cancelled", reason="Test release of unused reservation")
    with x.db.read() as s:
        assert available(s, s.get(Portion, pid)) == 2000000
    op, h, _ = prepare(x, "packaging", [pid], specification_id=spec)
    h.start(x.uid, op)
    with pytest.raises(DomainError, match="precision|precise"):
        h.execute(x.uid, "bad-pack-precision", op, dict(pack_masses=["1.000001", "0.999999"], remainder="0", loss="0",
                  evidence={**x.evidence, "precision_mg": 1000}, reason="Invalid fixture", physical_confirmation=True))
    with x.db.read() as s:
        assert balance(s, pid) == 2000000 and s.get(Portion, pid).status == "wip"


def test_full_chain_demo_is_repeatable_and_isolated(system, factory):
    lot = seed_material_demo(system.db, system.uid)
    assert seed_material_demo(system.db, system.uid) == lot
    with system.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Pack)) == 6
        assert s.scalar(select(func.count()).select_from(ProductLot)) == 1
        assert sum(balance(s, p.id) for p in s.scalars(select(Portion))) == 43000000
    for table in ("handling_plans", "product_lots", "packs"):
        with pytest.raises(DBAPIError):
            with system.db.write() as s:
                s.execute(text(f"DELETE FROM {table}"))
    real = factory("real")
    with pytest.raises(ValueError, match="isolated"):
        seed_material_demo(real.db, real.uid)
