"""Gross weighing is reviewed before posting; predictions never rewrite evidence."""
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from beanosort.models import Audit, Container, Destination, Ledger, Measurement, Operation, Particle, Profile, Reconciliation, User
from beanosort.quantities import DomainError
from beanosort.simulation import SimulationConfiguration, current_settings
from beanosort.weighing import build_review, predicted_mass, weighing_context
from test_references import reference
from test_web import client_for, token


def change_settings(x, percent="99", mean="0.15"):
    with x.db.read() as s: previous = current_settings(s).id
    return SimulationConfiguration(x.db).save(x.uid, "settings-"+percent+mean,
        {"previous_id": previous, "capture_percent": percent, "mean_mass_g": mean,
         "material_label": "Green coffee test assumption", "reason": "Explicit test parameter change"})


def ready(x, tmp_path, *, count=300, interrupted=False, percent=None, finish=True):
    if percent is not None: change_settings(x, percent)
    _, rid, _ = reference(x, tmp_path)
    pid, source, *_ = x.receive("3")
    old, data = x.prepare(pid, "1")
    x.workflow.transition(x.uid, old, "cancelled", reason="Use photographed beans")
    data.pop("capture_model_id")  # Pin current mass/capture Simulation Settings.
    op = x.workflow.prepare(x.uid, "weighing-job", {**data, "recipe_id": rid})
    run = x.workflow.start(x.uid, op, count=count, seed="gross-weighing")
    if interrupted:
        x.workflow.simulate_next(x.uid, run)
        x.workflow.transition(x.uid, op, "failed", reason="Test interrupted feeding; all retained material will be weighed")
    else:
        while x.workflow.simulate_next(x.uid, run):
            pass
    if finish:
        x.finish(op)
    return op, run, source


def form_for(x, op, **changes):
    with x.db.read() as s:
        context = weighing_context(s, s.get(Operation, op), x.db.environment)
        form = {"retained": "0", "loss": "0", "notes": "Container gross weights checked"}
        for row in context["rows"]:
            stream = row["stream"]
            form.update({"gross_"+stream: {"selected": "0.600", "pass_through": "1.300", "remainder": "2.500"}[stream],
                         "weighed_"+stream: "on", "revision_"+stream: str(row["container"].revision)})
    return {**form, **changes}


def review_for(x, op, form):
    with x.db.read() as s:
        return build_review(s, s.get(Operation, op), x.db.environment, form)


def particle_snapshot(x, run):
    with x.db.read() as s:
        return [(p.id, p.payload, p.simulated_destination) for p in s.scalars(select(Particle).filter_by(run_id=run).order_by(Particle.id))]


def test_area_mass_prefill_and_inverse_count_keep_particle_evidence(system, tmp_path):
    x = system; op, run, _ = ready(x, tmp_path)
    before = particle_snapshot(x, run)
    with x.db.read() as s:
        context = weighing_context(s, s.get(Operation, op), "simulation")
        model = context["model"]
        assert model["available"] and model["capture_probability"] == "0.99"
        assert model["mean_bean_mass_ug"] == 150000
        assert Decimal(model["sample_expansion_factor"]) > 20  # Explicit small-run sample expansion.
        assert model["populations"]["remainder"]["predicted_net_mg"] == 2000000
        assert context["rows"][2]["gross_value"] == "2.500"
        expected_mass = sum(Decimal(p[1]["features"]["area_mm2"]) for p in before)*Decimal(150)/Decimal(model["mean_area_mm2"])*Decimal(model["sample_expansion_factor"])
        predicted_total = sum(model["populations"][stream]["predicted_net_mg"] for stream in ("selected", "pass_through"))
        assert abs(Decimal(predicted_total)-expected_mass) <= 1000  # Only display rounding; never forced balance.
        assert Decimal(model["populations"]["selected"]["mean_bean_mass_mg"]) < 150
    preview = review_for(x, op, form_for(x, op))
    assert preview["discrepancy_mg"] == 100000
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
        assert s.scalar(select(func.count()).select_from(Reconciliation)) == 0
    x.workflow.reconcile(x.uid, "gross-accept", op, preview["data"])
    with pytest.raises(DomainError, match="pinned policy"):
        x.workflow.release(x.uid, op, accept_estimates=True, reason="No exception accepted")
    x.workflow.release(x.uid, op, accept_estimates=True, reason="Approve signed simulation shortfall", approve_discrepancy=True)
    assert x.workflow.reconcile(x.uid, "gross-accept", op, preview["data"])
    x.workflow.release(x.uid, op, accept_estimates=True, reason="Retry", approve_discrepancy=True)
    assert particle_snapshot(x, run) == before
    with x.db.read() as s:
        profile = s.scalar(select(Profile).filter_by(operation_id=op, stream="selected"))
        estimate = profile.data["mass_count_estimate"]
        assert Decimal(estimate["estimated_count"])*Decimal(estimate["mean_bean_mass_mg"]) == pytest.approx(Decimal(100000))
        assert estimate["evidence"] == "calculated_prediction" and estimate["physical_census"] is False
        assert profile.data["counts"]["tracked"] == 300
        assert profile.data["counts"]["physical_confirmed"] == 0
        measurements = {m.role: m for m in s.scalars(select(Measurement).filter_by(operation_id=op))}
        assert measurements["selected"].mass_mg == 100000
        assert measurements["selected"].provenance["gross_mg"] == 600000
        assert measurements["selected"].provenance["tare_mg"] == 500000
        assert s.get(Operation, op).status == "completed"
        approvals = list(s.scalars(select(Audit).filter_by(operation_id=op, action="simulation_discrepancy_approved")))
        assert len(approvals) == 1 and approvals[0].data["discrepancy_mg"] == 100000


@pytest.mark.parametrize("changes", [
    {"weighed_selected": ""}, {"weighed_pass_through": ""}, {"weighed_remainder": ""},
    {"gross_selected": ""}, {"gross_pass_through": ""}, {"gross_remainder": ""},
    {"gross_selected": "0.499"}, {"gross_remainder": "NaN"},
    {"gross_pass_through": "1.2345678"}, {"revision_selected": "0"},
])
def test_every_container_must_have_valid_current_gross_weighing(system, tmp_path, changes):
    x = system; op, _, _ = ready(x, tmp_path, count=30)
    with pytest.raises(DomainError):
        review_for(x, op, form_for(x, op, **changes))
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Reconciliation)) == 0


def test_negative_difference_precision_and_stale_tare_cannot_be_accepted(system, tmp_path):
    x = system; op, _, _ = ready(x, tmp_path)
    context = review_for(x, op, form_for(x, op, gross_selected="0.700001", gross_pass_through="1.500"))
    assert context["discrepancy_mg"] == -200001
    assert context["data"]["selected"] == "0.200001"
    proof = context["data"]["container_weighing"]["containers"]["selected"]
    with x.db.write() as s:
        container = s.get(Container, proof["container_id"])
        container.tare_mg = 600000; container.revision += 1
    with pytest.raises(DomainError, match="changed"):
        x.workflow.reconcile(x.uid, "stale-weighing", op, context["data"])
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Ledger)) == 1
        assert s.scalar(select(func.count()).select_from(Reconciliation)) == 0


def test_review_is_signed_requires_acceptance_and_retries_do_not_post_twice(system, tmp_path):
    x = system; op, run, _ = ready(x, tmp_path)
    client = client_for(x); base = "/jobs/"+op+"/weigh"
    page = client.get(base)
    assert page.status_code == 200 and "gross weight, including the container" in page.text
    form = {**form_for(x, op), "csrf": token(page), "key": token(page, "key")}
    assert client.post(base+"/review", data={**form, "csrf": "bad"}).status_code == 403
    page = client.post(base+"/review", data=form)
    assert page.status_code == 200, page.text
    assert "+0.100 kg" in page.text
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Reconciliation)) == 0
    accept = {"csrf": token(page), "review_token": token(page, "review_token"), "accept_review": "on", "reason": "Accept simulation discrepancy"}
    assert client.post(base+"/accept", data={**accept, "accept_review": ""}).status_code == 409
    assert client.post(base+"/accept", data={**accept, "review_token": accept["review_token"]+"tamper"}).status_code == 409
    assert client.post("/jobs/not-this-job/weigh/accept", data=accept).status_code == 409
    response = client.post(base+"/accept", data=accept)
    assert response.status_code == 200 and "Completed" in response.text, response.text
    assert client.post(base+"/accept", data=accept).status_code == 200
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Reconciliation)) == 1
        assert s.scalar(select(func.count()).select_from(Particle).where(Particle.run_id == run)) == 300
        assert s.scalar(select(func.count()).select_from(Ledger)) == 5  # receipt, consumed source, 3 children
    client.close()


def test_legacy_prepared_job_reviews_area_yield_without_overwriting_weights(system, tmp_path):
    x = system; op, run, _ = ready(x, tmp_path)
    before = particle_snapshot(x, run)
    # Jobs prepared before occupied-destination support have no baseline fields.
    with x.db.write() as s:
        for destination in s.scalars(select(Destination).filter_by(operation_id=op)):
            destination.readiness = {key: value for key, value in destination.readiness.items()
                                     if key not in {"existing_mass_mg", "ready_to_receive"}}
    with x.db.read() as s:
        context = weighing_context(s, s.get(Operation, op), 'simulation')
        selected = context['model']['populations']['selected']
        expected_mg = Decimal(selected['expected_count'])*Decimal(selected['mean_bean_mass_mg'])
        assert abs(Decimal(selected['predicted_net_mg'])-expected_mg) <= 500
        assert Decimal(selected['mean_bean_mass_mg']) < 150  # Small beans weigh less per bean.
    client = client_for(x); base = '/jobs/'+op+'/weigh'
    page = client.get(base)
    assert page.status_code == 200 and 'data-yield-explanation="selected"' in page.text
    assert 'expected beans' in page.text and 'mg per bean' in page.text
    page = client.post(base+'/review', data={**form_for(x, op), 'csrf': token(page), 'key': token(page, 'key')})
    assert page.status_code == 200 and '3.000 kg' in page.text
    assert client.post(base+'/accept', data={'csrf': token(page), 'review_token': token(page, 'review_token'),
        'accept_review': 'on', 'reason': 'Reviewed legacy job independently of its predicted mass'}).status_code == 200
    with x.db.read() as s:
        assert s.scalar(select(Measurement).filter_by(operation_id=op, role='selected')).mass_mg == 100000
        assert s.scalar(select(Reconciliation).filter_by(operation_id=op)).accounting['input_mg'] == 3000000
    assert particle_snapshot(x, run) == before
    client.close()


def test_remainder_zero_is_confirmed_and_real_release_never_bypasses_policy(factory):
    x = factory("real"); pid, *_ = x.receive("3")
    op, _ = x.prepare(pid, "3"); x.start(op); x.finish(op)
    with x.db.read() as s:
        assert predicted_mass(s, s.get(Operation, op), "real")["available"] is False
    preview = review_for(x, op, form_for(x, op, gross_remainder="0.5"))
    assert preview["data"]["remainder"] == "0"
    assert preview["rows"][2]["count_estimate"]["estimated_count"] == "0"
    x.workflow.reconcile(x.uid, "real-gross-record", op, preview["data"])
    with pytest.raises(DomainError, match="pinned policy"):
        x.workflow.release(x.uid, op, accept_estimates=True, reason="Cannot override real discrepancy", approve_discrepancy=True)


def test_operator_records_weighing_supervisor_explicitly_approves_discrepancy(system, tmp_path):
    x = system; op, _, _ = ready(x, tmp_path)
    client = client_for(x); base = "/jobs/"+op
    with x.db.write() as s: s.get(User, x.uid).role = "operator"
    page = client.get(base+"/weigh")
    page = client.post(base+"/weigh/review", data={**form_for(x, op), "csrf": token(page), "key": token(page, "key")})
    response = client.post(base+"/weigh/accept", data={"csrf": token(page), "review_token": token(page, "review_token"), "accept_review": "on"})
    assert response.status_code == 200
    with x.db.read() as s: assert s.get(Operation, op).release_status == "held"
    with x.db.write() as s: s.get(User, x.uid).role = "supervisor"
    data = {"csrf": token(response), "accept_estimates": "on", "reason": "Reviewed simulated shortfall"}
    assert client.post(base+"/release", data=data).status_code == 409
    assert client.post(base+"/release", data={**data, "accept_discrepancy": "on"}).status_code == 200
    with x.db.read() as s: assert s.get(Operation, op).status == "completed"
    client.close()


@pytest.mark.parametrize("percent", ["0", "100"])
def test_capture_extremes_and_pinned_settings_keep_prediction_conservative(system, tmp_path, percent):
    x = system; op, _, _ = ready(x, tmp_path, percent=percent)
    with x.db.read() as s:
        before = predicted_mass(s, s.get(Operation, op), "simulation")
    selected = before["populations"]["selected"]
    if percent == "0":
        assert Decimal(selected["expected_count"]) == 0 and selected["predicted_net_mg"] == 0
        assert selected["mean_bean_mass_mg"] is None
    else:
        assert Decimal(selected["expected_count"]) > 0
    expanded_count = sum(Decimal(before["populations"][k]["expected_count"]) for k in ("selected", "pass_through"))
    assert expanded_count == pytest.approx(Decimal(1000000)/150)
    change_settings(x, "25", "0.3")
    with x.db.read() as s:
        assert predicted_mass(s, s.get(Operation, op), "simulation") == before


def test_interrupted_reference_run_keeps_unprocessed_source_and_unknown_coverage(system, tmp_path):
    x = system; op, _, _ = ready(x, tmp_path, count=503, interrupted=True)
    with x.db.read() as s:
        operation = s.get(Operation, op)
        model = predicted_mass(s, operation, "simulation")
        assert model["recorded_passages"] == 250 and model["declared_passages"] == 503
        assert model["populations"]["remainder"]["predicted_net_mg"] == 2503000
        profile = x.workflow.profile(s, operation)
        assert profile["missing_source_particles"] == 253
        assert profile["coverage"] == "incomplete reference-resampled simulation"


def test_unknown_historical_tare_is_never_guessed_and_only_saved_on_accept(system, tmp_path):
    x = system; op, _, _ = ready(x, tmp_path)
    form = form_for(x, op)
    with x.db.read() as s:
        cid = weighing_context(s, s.get(Operation, op), "simulation")["rows"][0]["container"].id
    # Reproduce a legacy missing-tare vessel solely in the disposable test schema.
    with x.db.write() as s:
        if x.db.engine.dialect.name == "sqlite":
            guard = s.execute(text("SELECT sql FROM sqlite_master WHERE name='container_weights_not_cleared'")).scalar_one()
            s.execute(text("DROP TRIGGER container_weights_not_cleared"))
        else:
            s.execute(text("ALTER TABLE containers DISABLE TRIGGER container_weights_required"))
        s.execute(text("UPDATE containers SET tare_mg=NULL WHERE id=:id"), {"id": cid})
        if x.db.engine.dialect.name == "sqlite": s.execute(text(guard))
        else: s.execute(text("ALTER TABLE containers ENABLE TRIGGER container_weights_required"))
    with pytest.raises(DomainError): review_for(x, op, form)
    preview = review_for(x, op, {**form, "tare_selected": "0.600", "gross_selected": "1000.700"})
    with pytest.raises(DomainError, match="capacity"):
        x.workflow.reconcile(x.uid, "rollback-tare", op, preview["data"])
    with x.db.read() as s: assert s.get(Container, cid).tare_mg is None
    preview = review_for(x, op, {**form, "tare_selected": "0.600", "gross_selected": "0.700"})
    with x.db.read() as s: assert s.get(Container, cid).tare_mg is None
    x.workflow.reconcile(x.uid, "accept-tare", op, preview["data"])
    with x.db.read() as s:
        assert s.get(Container, cid).tare_mg == 600000 and s.get(Container, cid).revision == 2
        evidence = s.scalar(select(Measurement).filter_by(operation_id=op, role="selected")).provenance
        assert evidence["container_revision"] == 2 and evidence["tare_mg"] == 600000
