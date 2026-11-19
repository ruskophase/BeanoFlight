"""Container weighing and explicit area-proportional simulation mass predictions.

Predictions never alter particle evidence, photographs or entered measurements.
All inventory arithmetic is integer milligrams; derived model values use Decimal.
"""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select

from .capture import probability
from .models import Assignment, Container, Destination, Input, Operation, Particle, Reconciliation, Run, SimulationAssumption, Version
from .quantities import DomainError, kg, mass

MODEL = "mean-area-proportional-bean-mass/v1"
STREAMS = ("selected", "pass_through", "remainder")


def kg_entry(value):
    return format(Decimal(value)/1000000, '.3f') if value % 1000 == 0 else kg(value)


def predicted_mass(session, operation, environment, *, collection=None):
    result = {"version": MODEL, "available": False, "evidence": "calculated_prediction",
              "reason": "No pinned reference area/mean-mass model is available; enter actual gross weights.",
              "populations": {stream: {"predicted_net_mg": None, "mean_bean_mass_mg": None,
                                       "expected_count": None} for stream in STREAMS}}
    run = session.scalar(select(Run).filter_by(operation_id=operation.id))
    assumption = session.scalar(select(SimulationAssumption).filter_by(operation_id=operation.id))
    from .population_sources import input_profile
    population_input = input_profile(session, operation.id)
    if environment != "simulation" or not run or not (assumption or population_input) or not run.source_versions.get("reference_population"):
        return result
    from .references import load_dataset, metric_statistics, validate_recipe
    dataset = validate_recipe(session, session.get(Version, operation.recipe_id).data)
    beans, _ = load_dataset(dataset)
    by_sequence = {row["bean_sequence"]: row for row in beans}
    area = metric_statistics(beans, "area_mm2")
    if not area["count"] or not area["mean"]:
        return result
    source_model = population_input.data["mass_model"] if population_input else {}
    mean_area = Decimal(source_model.get("reference_mean_area_mm2") or str(area["mean"]))
    mean_mass_ug = source_model.get("reference_mean_mass_ug") or assumption.data["mean_mass_ug"]
    mean_mass = Decimal(mean_mass_ug)/1000
    input_mean_mass = Decimal(source_model.get("input_mean_bean_mass_mg") or str(mean_mass))
    # A deliberately smaller test count is a sample, not a new physical census.
    # Expansion is recorded explicitly and never adds particle/photo records.
    expansion = Decimal(operation.planned_mg)/input_mean_mass/Decimal(run.expected_particles)
    p = probability(session.get(Version, operation.capture_model_id).data)
    sequence = Particle.payload["reference_bean"]["bean_sequence"].as_integer()
    decision = Particle.payload["decision"].as_string()
    action = Particle.payload["action"].as_string()
    query=select(sequence, decision, action, func.count(Particle.id)).where(Particle.run_id == run.id)
    if collection:
        from .segments import particle_filter
        condition=particle_filter(session,collection)
        if condition is None:
            result["reason"]="Local particle boundary unknown; this collection's composition/count prediction is unavailable."
            return result
        query=query.where(condition)
    from .run_summaries import get_summary, reference_groups
    summary = get_summary(session, run.id, collection)
    groups = reference_groups(summary) if summary else session.execute(query.group_by(sequence, decision, action)).all()
    totals = {stream: {"count": Decimal(0), "mass": Decimal(0), "unknown": Decimal(0)} for stream in STREAMS[:2]}
    tracked, unresolved = 0, 0
    for seq, choice, issued, count in groups:
        tracked += count
        if choice == "selected" and issued == "attempted":
            weights = {"selected": p, "pass_through": 1-p}
        elif choice == "pass":
            weights = {"selected": Decimal(0), "pass_through": Decimal(1)}
        else:
            unresolved += count
            continue
        value = by_sequence.get(seq, {}).get(area["field"])
        for stream, fraction in weights.items():
            weight = Decimal(count)*expansion*fraction
            totals[stream]["count"] += weight
            if value is None:
                totals[stream]["unknown"] += weight
            else:
                totals[stream]["mass"] += weight*mean_mass*Decimal(str(value))/mean_area
    if not tracked:
        if collection is not None:
            result.update(available=True,reason="No simulated passages in this collection window; not a physical empty-bin confirmation")
            for stream in STREAMS[:2]:result["populations"][stream].update(predicted_net_mg=0,unrounded_net_mg="0",expected_count="0")
        else:
            result["reason"] = "No generated passages yet; automatic weighing predictions are unavailable."
        return result
    result.update(available=not unresolved and not any(t["unknown"] for t in totals.values()), reason="Whole reference records, linear area-to-mass assumption; not measured bean masses.",
        dataset_id=dataset.id, dataset_fingerprint=dataset.fingerprint, area_field=area["field"],
        mean_area_mm2=str(mean_area), mean_bean_mass_ug=mean_mass_ug,
        settings_id=assumption.settings_id if assumption else population_input.data.get("count_basis", {}).get("settings_id"), capture_model_id=operation.capture_model_id,
        capture_probability=str(p), recorded_passages=tracked, declared_passages=run.expected_particles,
        sample_expansion_factor=str(expansion), unresolved_passages=unresolved,
        assumptions=["mass proportional to projected area, not volume",
                     "reference mean area corresponds to the pinned mean bean mass",
                     "generated passages represent the planned processed population",
                     "unprocessed remainder uses the retained source mean mass" if population_input else "unprocessed remainder uses the reference-wide mean mass",
                     "weight-based count estimates assume representative stream composition",
                     "original particle outcomes and photographs are retained unchanged"],
        rounding="individual net predictions rounded to 0.001 kg; discrepancies remain visible")
    if population_input:
        result.update(input_population_profile_id=population_input.id,
                      input_mean_bean_mass_mg=str(input_mean_mass), source_population_fingerprint=population_input.data["fingerprint"],
                      mass_assumption_settings_id=source_model.get("mass_assumption_settings_id"))
    for stream, total in totals.items():
        known = not unresolved and not total["unknown"]
        predicted = int((total["mass"]/1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))*1000 if known else None
        result["populations"][stream] = {"predicted_net_mg": predicted,
            "unrounded_net_mg": str(total["mass"]) if known else None,
            "expected_count": str(total["count"]), "unknown_area_count": str(total["unknown"]),
            "mean_bean_mass_mg": str(total["mass"]/total["count"]) if known and total["count"] else None}
    remainder = Decimal(operation.boundary_mg)-Decimal(operation.planned_mg)*tracked/Decimal(run.expected_particles)
    result["populations"]["remainder"] = {
        "unrounded_net_mg": str(remainder),
        "predicted_net_mg": int((remainder/1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))*1000,
        "expected_count": str(remainder/input_mean_mass), "mean_bean_mass_mg": str(input_mean_mass)}
    from .sorting_modes import source_return
    from .segments import current
    if source_return(session, operation) and collection is None and current(session,operation) is None:
        result["source_return"] = True
        result["assumptions"].append("Shared source/Pass-through retains the whole input mass; generated passages do not census material remaining in the vessel")
        result["populations"]["pass_through"].update(predicted_net_mg=operation.boundary_mg,
            unrounded_net_mg=str(operation.boundary_mg),
            expected_count=str(Decimal(operation.boundary_mg)/input_mean_mass), mean_bean_mass_mg=str(input_mean_mass))
        result["populations"]["remainder"].update(predicted_net_mg=0, unrounded_net_mg="0", expected_count="0")
    return result


def container_rows(session, operation):
    if operation.kind != "sorting" or operation.status not in {"finishing", "awaiting_reconciliation"} or session.scalar(select(Reconciliation.id).filter_by(operation_id=operation.id)):
        raise DomainError("Finish processing before weighing; final weights must precede the first reconciliation")
    from .collections import feed_input
    source = session.scalar(select(Assignment).filter_by(portion_id=feed_input(session, operation).portion_id, active=True))
    if not source:
        raise DomainError("The source container assignment changed; inspect this job")
    ids = {d.stream: d.container_id for d in session.scalars(select(Destination).filter_by(operation_id=operation.id))}
    ids["remainder"] = source.container_id
    from .sorting_modes import hopper_mode, source_return
    from .segments import current
    if current(session,operation):
        from .segmented_weighing import collection_rows
        return collection_rows(session,operation,source)
    streams = ("pass_through",) if source_return(session, operation) else ("pass_through", "remainder") if hopper_mode(session, operation) else STREAMS
    return [{"stream": stream, "shared_source":len(streams)==1,
             "title": "Source / Pass-through — final contents" if len(streams)==1 else {"selected": "Selected output", "pass_through": "Pass-through output", "remainder": "Source container — beans remaining"}[stream],
             "container": session.get(Container, ids[stream])} for stream in streams]


def weighing_context(session, operation, environment):
    from .collections import baselines, starting_mass
    rows = container_rows(session, operation)
    model = predicted_mass(session, operation, environment)
    from .segments import current
    if current(session,operation):
        from .segmented_weighing import collection_context
        return collection_context(session,operation,environment,rows,model)
    prior = baselines(session, operation)
    for row in rows:
        predicted = model["populations"][row["stream"]]["predicted_net_mg"]
        baseline = prior.get(row["stream"], {}).get("existing_mass_mg", 0)
        row["starting_mass_mg"] = baseline
        row["predicted_added_mg"] = predicted
        if predicted is not None: predicted += baseline
        row["predicted_net_mg"] = predicted
        row["gross_value"] = kg_entry(predicted+row["container"].tare_mg) if predicted is not None and row["container"].tare_mg is not None else ""
    from .sorting_modes import hopper_mode
    return {"rows": rows, "model": model, "input_total_mg":operation.boundary_mg+starting_mass(session,operation),"hopper_emptying":hopper_mode(session,operation)}


def count_estimate(amount, model, stream):
    mean = model["populations"][stream]["mean_bean_mass_mg"]
    count = str(Decimal(amount)/Decimal(mean)) if mean else "0" if amount == 0 else None
    return {"method": MODEL, "estimated_count": count,
            "count_per_kg": str(Decimal(1000000)/Decimal(mean)) if mean else None,
            "mean_bean_mass_mg": mean, "mass_basis_mg": amount, "evidence": "calculated_prediction",
            "predicted_count_before_weighing": model["populations"][stream]["expected_count"],
            "physical_census": False, "assumption": "area/mass model and representative expected stream composition",
            "particle_and_photo_records_changed": False}


def build_review(session, operation, environment, form):
    from .collections import baselines, preview_count
    context = weighing_context(session, operation, environment)
    prior=baselines(session,operation)
    segmented=context.get("segmented",False)
    weights, quantities = {}, ({row["stream"]:"0" for row in context["rows"]} if segmented else {stream:"0" for stream in STREAMS})
    quantities.setdefault("remainder","0")
    for row in context["rows"]:
        stream, container = row["stream"], row["container"]
        if form.get("weighed_"+stream) != "on":
            raise DomainError("Confirm the weight of every container, including an empty source container")
        if str(form.get("revision_"+stream)) != str(container.revision):
            raise DomainError("Container details changed; reload and weigh using its current tare")
        tare = container.tare_mg
        if tare is None:
            tare = mass(form.get("tare_"+stream))
        gross = mass(form.get("gross_"+stream), zero=True)
        if gross < tare:
            raise DomainError(f"Gross weight of {container.label} cannot be less than its tare")
        net = gross-tare
        quantities[stream] = kg(net)
        weights[stream] = {"container_id": container.id, "label": container.label,
            "revision": container.revision, "registered_tare_mg": container.tare_mg,
            "effective_tare_revision": container.revision + (1 if container.tare_mg is None else 0),
            "tare_mg": tare, "gross_mg": gross, "net_mg": net, "weighed_confirmation": True,
            "starting_mass_mg":row["starting_mass_mg"], "starting_portion_id":prior.get(stream,{}).get("existing_portion_id")}
        if segmented:
            saved=row.get("saved_weight")
            weights[stream].update(collection_id=row.get("collection_id"),saved_weight_id=saved.id if saved else None,
                saved_weight_reused=bool(saved and gross==saved.gross_mg and tare==saved.tare_mg))
        if net < row["starting_mass_mg"]:
            raise DomainError("Final destination mass cannot be below the starting contents in this addition workflow")
        estimate = count_estimate(net-row["starting_mass_mg"], context["model"], stream)
        if row["starting_mass_mg"]:
            estimate=preview_count(session,prior[stream],net,estimate)
        row.update(weights[stream], count_estimate=estimate, added_mass_mg=None if row["shared_source"] else net-row["starting_mass_mg"])
    quantities.update(retained=form.get("retained") or "0", loss=form.get("loss") or "0", sample="0")
    for name in ("retained", "loss"): mass(quantities[name], zero=True)
    discrepancy = context["input_total_mg"]-sum(mass(value, zero=True) for value in quantities.values())
    proof = {"schema": "beanosort-container-weighing/v2" if segmented else "beanosort-container-weighing/v1", "containers": weights,
             "model": context["model"], "sampling": "not used; no sample removed",
             "discrepancy_mg": discrepancy, "dependence_group": operation.id}
    if segmented:proof["segment_id"]=context["segment_id"]
    data = {**quantities, "reason": form.get("notes") or "Container gross weights reviewed; tare deducted separately; sampling not used",
            "evidence": {"kind": "simulation" if environment == "simulation" else "direct_measurement",
                         "method": "operator-entered gross container weights", "tare_basis": "gross minus recorded/entered container tare"},
            "container_weighing": proof}
    if segmented:
        data["collection_masses"]={row["stream"]:quantities[row["stream"]] for row in context["rows"] if row.get("collection_id")}
    context.update(data=data, discrepancy_mg=discrepancy, accounted_mg=context["input_total_mg"]-discrepancy)
    return context


def validate_and_record_tares(session, user, operation, original, proof, quantities, environment):
    from .service import audit, check_revision, container_snapshot, get
    expected_rows = container_rows(session, operation)
    from .segments import current,latest_weight
    segment=current(session,operation)
    schema="beanosort-container-weighing/v2" if segment else "beanosort-container-weighing/v1"
    if proof.get("schema") != schema or set(proof.get("containers", {})) != {row["stream"] for row in expected_rows}:
        raise DomainError("Every source/output container needs a complete weighing record")
    ids = {d.stream: d.container_id for d in session.scalars(select(Destination).filter_by(operation_id=operation.id))}
    ids["remainder"] = original.container_id
    from .collections import baselines
    prior=baselines(session,operation)
    for stream, value in proof["containers"].items():
        container = get(session, Container, value["container_id"])
        if container.id != ids[stream] or value.get("weighed_confirmation") is not True:
            raise DomainError("Weighing does not match this job's confirmed containers")
        baseline=prior.get(stream,{})
        if segment:
            if proof.get("segment_id")!=segment.id:raise DomainError("Collection assignments changed; review again")
            row=next(row for row in expected_rows if row["stream"]==stream)
            if value.get("collection_id")!=row.get("collection_id") or value.get("saved_weight_id")!=(row["saved_weight"].id if row.get("saved_weight") else None):
                raise DomainError("Recorded collection weights changed; review again")
        if value.get("starting_mass_mg",0)!=baseline.get("existing_mass_mg",0) or value.get("starting_portion_id")!=baseline.get("existing_portion_id"):
            raise DomainError("Starting destination contents changed; review the final weighings again")
        check_revision(container, value.get("revision"))
        if container.tare_mg != value.get("registered_tare_mg"):
            raise DomainError("Container tare changed; review the weighings again")
        tare = mass(str(value["tare_mg"]), "mg")
        gross = mass(str(value["gross_mg"]), "mg", zero=True)
        if gross-tare != quantities[stream]:
            raise DomainError("Net container mass must equal gross minus tare")
        if container.tare_mg is None:
            before = container_snapshot(container)
            container.tare_mg = tare
            container.revision += 1
            audit(session, user, "container_updated", {"container_id": container.id, "before": before,
                  "after": container_snapshot(container), "reason": "Missing tare entered during container reconciliation",
                  "tare_mg": tare, "tare_evidence": "simulation" if environment == "simulation" else "operator_declaration",
                  "tare_method": "empty-container tare entered for gross-weight reconciliation", "unit": "mg"}, station=operation.station_id)
        elif tare != container.tare_mg:
            raise DomainError("Recorded container tare cannot be overridden during weighing")
