from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.capture import expected_profile, fixture_particle
from beanosort.models import (Audit, Ledger, MonitorEvent, Operation, Particle, Portion, Profile,
                             Run, SimulationAssumption, SimulationSettings, User, Version)
from beanosort.quantities import DomainError, mass
from beanosort.service import balance
from beanosort.simulation import SimulationConfiguration, current_settings, estimate_particles
from test_web import client_for, token


def settings_for(x):
    with x.db.read() as s:
        return current_settings(s)


def change(x, **extra):
    data = dict(previous_id=settings_for(x).id, mean_mass_g="0.15", capture_percent="99",
                material_label="Green coffee", reason="Reviewed test assumption")
    data.update(extra)
    return SimulationConfiguration(x.db).save(x.uid, "settings-" + uuid4().hex, data)


def prepare_current(x, pid, planned="20", **extra):
    destinations = [x.workflow.create_container(x.uid, label="Dest " + uuid4().hex, location="Test", capacity="1000", tare_mass="0.5") for _ in range(2)]
    data = dict(portion_id=pid, planned_mass=planned, station_id=x.stations["Training sorter"],
                selected_id=destinations[0], pass_through_id=destinations[1], ready=True,
                recipe_id=x.versions["recipe"], policy_id=x.versions["reconciliation_policy"])
    data.update(extra)
    return x.workflow.prepare(x.uid, "prepare-" + uuid4().hex, data)


def test_researched_default_decimal_estimates_and_99_percent(system):
    x = system
    settings = settings_for(x)
    assert settings.mean_mass_ug == 150000
    assert settings.material_label == "Green coffee"
    for value, unit in (("1", "kg"), ("1000", "g"), ("1000000", "mg")):
        result = estimate_particles(mass(value, unit), settings)
        assert result["rounded_particles"] == 6667
        assert Decimal(result["estimated_particles"]) == Decimal(1000000) / 150
        assert result["evidence"] == "calculated_prediction"
        assert not result["physical_census"]
    assert estimate_particles(mass("20"), settings)["rounded_particles"] == 133333
    assert estimate_particles(75, settings)["rounded_particles"] == 1  # half up
    with x.db.read() as s:
        model = s.get(Version, settings.capture_model_id).data
        assert model["probability"] == "0.99"
    profile = expected_profile([{"payload": fixture_particle(i)} for i in range(10000)], model)
    assert profile["expected"] == {"selected": "1980.00", "pass_through": "8020.00", "unresolved": "0"}
    assert profile["counts"]["physical_confirmed"] == 0


def test_settings_validation_atomicity_and_immutability(system):
    x = system
    initial = settings_for(x)
    for value in ("0", "-1", "NaN", "Infinity", "1e9999", "1001", "0.0000001", "0.1500000000000000000000000000000001", 0.15, True, None):
        with pytest.raises(DomainError):
            change(x, mean_mass_g=value)
    for value in ("-0.1", "100.01", "NaN", "Infinity", "99.00001", 0.99):
        with pytest.raises(DomainError):
            change(x, capture_percent=value)
    for extra in ({"material_label": ""}, {"reason": " "}, {"material_label": "x" * 121}):
        with pytest.raises(DomainError):
            change(x, **extra)
    assert settings_for(x).id == initial.id
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(SimulationSettings)) == 1
    for query in ("UPDATE simulation_settings SET mean_mass_ug=200000", "DELETE FROM simulation_settings"):
        with pytest.raises(DBAPIError):
            with x.db.engine.begin() as c:
                c.execute(text(query))


def test_receipt_estimate_is_pinned_and_does_not_change_stock(system):
    x = system
    initial = settings_for(x)
    pid, _, key, data = x.receive("1", bean_type="Green coffee", simulation_settings_id=initial.id)
    with x.db.read() as s:
        op = s.get(Portion, pid).created_by
        basis = s.scalar(select(SimulationAssumption).filter_by(operation_id=op)).data
    assert basis["rounded_particles"] == 6667
    change(x, mean_mass_g="0.2")
    assert x.workflow.receive(x.uid, key, data) == pid
    with x.db.read() as s:
        assert balance(s, pid) == 1000000
        assert s.scalar(select(SimulationAssumption).filter_by(operation_id=op)).data == basis
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
    with pytest.raises(DBAPIError):
        with x.db.engine.begin() as c:
            c.execute(text("DELETE FROM simulation_assumptions"))
    unknown, *_ = x.receive("1")
    with x.db.read() as s:
        assert s.scalar(select(SimulationAssumption).filter_by(operation_id=s.get(Portion, unknown).created_by)) is None


def test_planned_mass_default_count_old_versions_replay_and_weight_independence(system):
    x = system
    pid, *_ = x.receive("20")
    op = prepare_current(x, pid, planned="0.15")  # 1,000 beans, not whole 20 kg
    initial = settings_for(x)
    change(x, mean_mass_g="0.2", capture_percent="100")
    run_id = x.workflow.start(x.uid, op, seed="pinned")
    with x.db.read() as s:
        run = s.get(Run, run_id)
        assert run.expected_particles == 1000
        assert run.source_versions["particle_count_estimate"]["settings_id"] == initial.id
        assert s.get(Operation, op).capture_model_id == initial.capture_model_id
    assert x.workflow.start(x.uid, op, seed="pinned") == run_id
    while x.workflow.simulate_next(x.uid, run_id):
        pass
    with x.db.read() as s:
        before = {p.source_key: p.simulated_destination for p in s.scalars(select(Particle).filter_by(run_id=run_id))}
        profile = x.workflow.profile(s, s.get(Operation, op))
        assert profile["expected"]["selected"] == "198.00"
        assert profile["expected"]["pass_through"] == "802.00"
        assert profile["counts"]["physical_confirmed"] == 0
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
    assert x.workflow.simulate_next(x.uid, run_id, replay_first=True) == 0
    x.finish(op)
    # Measured/synthetic entered masses are independent of predicted counts.
    x.reconcile(op, selected="0.02", pass_through="0.13", remainder="19.85")
    x.workflow.release(x.uid, op, accept_estimates=True, reason="Review synthetic results")
    assert x.workflow.simulate_next(x.uid, run_id, replay_first=True) == 0
    with x.db.read() as s:
        assert {p.source_key: p.simulated_destination for p in s.scalars(select(Particle).filter_by(run_id=run_id))} == before
        frozen = s.scalar(select(Profile).filter_by(operation_id=op, stream="run")).data
        assert frozen["source_versions"]["particle_count_estimate"]["mean_mass_g"] == "0.15"
        assert sum(s.scalars(select(Ledger.delta_mg))) == 20000000


@pytest.mark.parametrize("percent,selected", [("0", 0), ("100", 50)])
def test_settings_capture_extremes_and_monitor_evidence(system, percent, selected):
    x = system
    change(x, capture_percent=percent)
    pid, *_ = x.receive("1")
    op = prepare_current(x, pid, "1")
    run_id = x.workflow.start(x.uid, op, count=250)
    x.workflow.simulate_next(x.uid, run_id)
    with x.db.read() as s:
        profile = x.workflow.profile(s, s.get(Operation, op))
        assert profile["synthetic_monitor"]["counts"]["selected"] == selected
        assert profile["synthetic_monitor"]["counts"]["pass_through"] == 250 - selected
        assert {e.evidence for e in s.scalars(select(MonitorEvent))} == {"simulation", "calculated_prediction"}
        assert profile["counts"]["physical_confirmed"] == 0
        assert "operator-entered" in s.get(Run, run_id).source_versions["fixture_count_basis"]


def test_large_bag_estimate_not_capped_and_small_sample_override(system):
    x = system
    pid, *_ = x.receive("20")
    op = prepare_current(x, pid)
    run_id = x.workflow.start(x.uid, op)
    with x.db.read() as s:
        assert s.get(Run, run_id).expected_particles == 133333
    assert x.workflow.simulate_next(x.uid, run_id) == 250
    pid, *_ = x.receive("200")
    too_big = prepare_current(x, pid, "200")
    with pytest.raises(DomainError, match="never silently capped"):
        x.workflow.start(x.uid, too_big)
    with x.db.read() as s:
        assert s.get(Operation, too_big).status == "prepared"
    sample = x.workflow.start(x.uid, too_big, count=500)
    with x.db.read() as s:
        assert s.get(Run, sample).expected_particles == 500


def test_concurrent_settings_edits_and_idempotency(system):
    x = system
    config = SimulationConfiguration(x.db)
    data = dict(previous_id=settings_for(x).id, mean_mass_g="0.155", capture_percent="98.5",
                material_label="Green coffee", reason="Concurrent update test")
    def save(key):
        try:
            return config.save(x.uid, key, data)
        except DomainError:
            return "conflict"
    keys = ["concurrent-settings-a", "concurrent-settings-b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, keys))
    assert results.count("conflict") == 1
    i = next(i for i, result in enumerate(results) if result != "conflict")
    assert config.save(x.uid, keys[i], data) == results[i]
    assert settings_for(x).version == 2
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action == "simulation_settings_created")) == 2


def test_real_material_count_is_only_prediction_and_no_synthetic_execution(factory):
    x = factory("real")
    pid, *_ = x.receive("1", bean_type="Green coffee", simulation_settings_id=settings_for(x).id)
    op, _ = x.prepare(pid, "1")
    run_id = x.start(op)
    with x.db.read() as s:
        estimate = s.scalar(select(SimulationAssumption).filter_by(operation_id=s.get(Portion, pid).created_by)).data
        assert estimate["evidence"] == "calculated_prediction"
        assert s.get(Run, run_id).expected_particles is None
        assert s.get(Version, s.get(Operation, op).capture_model_id).data["probability"] == "0.97"
    with pytest.raises(DomainError, match="disabled for real"):
        x.workflow.simulate_next(x.uid, run_id)


def test_settings_web_forms_and_automatic_batches(system):
    x = system
    client = client_for(x)
    page = client.get("/simulation-settings")
    assert "99.00%" in page.text and "133,333" in page.text
    old_id = settings_for(x).id
    payload = dict(csrf=token(page), key=token(page, "key"), previous_id=old_id,
                   mean_mass_g="0.2", capture_percent="99", material_label="Green coffee", reason="UI change")
    assert client.post("/simulation-settings", data={**payload, "csrf": "bad"}).status_code == 403
    page = client.post("/simulation-settings", data=payload)
    assert page.status_code == 200 and "5,000" in page.text
    assert client.post("/simulation-settings", data=payload).status_code == 200
    current = settings_for(x)
    pid, *_ = x.receive("0.1", bean_type="Green coffee", simulation_settings_id=current.id)
    assert "≈ 500 particles" in client.get("/portions/" + pid).text
    op = prepare_current(x, pid, "0.1")
    page = client.get("/jobs/" + op)
    page = client.post(f"/jobs/{op}/start", data={"csrf": token(page), "count": "", "seed": "UI"})
    assert page.status_code == 200
    for total in (250, 500, 500):
        result = client.post(f"/jobs/{op}/simulate-batch", data={"csrf": token(page)})
        assert result.status_code == 200
        assert result.json()["received"] == total
        assert result.json()["evidence"] == "simulation"
    assert result.json()["complete"] is True
    page = client.get("/jobs/" + op)
    assert "99.00" in page.text and "Physical census" in page.text
    with x.db.write() as s:
        s.get(User, x.uid).role = "operator"
    page = client.get("/simulation-settings")
    assert "An administrator" in page.text
    assert client.post("/simulation-settings", data={**payload, "key": "operator-attempt", "previous_id": current.id}).status_code == 409
    assert settings_for(x).version == 2
    client.close()


def test_batch_deduplication_and_collisions_within_one_batch(system):
    x = system
    pid, *_ = x.receive("1")
    op = prepare_current(x, pid, "1")
    run_id = x.workflow.start(x.uid, op, count=500)
    first = fixture_particle(0)
    assert x.workflow.ingest(x.uid, run_id, "duplicate-row", [first, first]) == 1
    altered = {**fixture_particle(1), "key": first["key"]}
    with pytest.raises(DomainError, match="different data"):
        x.workflow.ingest(x.uid, run_id, "bad-identity", [fixture_particle(2), altered])
    collision = {**fixture_particle(2), "sequence": 1}
    with pytest.raises(DomainError, match="another particle"):
        x.workflow.ingest(x.uid, run_id, "bad-sequence", [fixture_particle(1), collision])
    # Out-of-order batches still fill the earliest gap without redrawing.
    assert x.workflow.ingest(x.uid, run_id, "later", [fixture_particle(300)]) == 1
    assert x.workflow.simulate_next(x.uid, run_id) == 249
    assert x.workflow.simulate_next(x.uid, run_id) == 249
    assert x.workflow.simulate_next(x.uid, run_id) == 0
