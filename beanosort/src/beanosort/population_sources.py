"""Immutable sampling populations for further passes through the simulator.

Weights describe a joint feature distribution, not extra stock or a physical census.
Each new passage gets a new identity and an independently seeded capture outcome.
"""
from bisect import bisect_right
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from functools import reduce
from math import gcd, lcm

from sqlalchemy import func, select

from .capture import digest
from .models import Input, Operation, Output, Particle, Portion, Profile, Reconciliation, Run, SimulationAssumption
from .quantities import DomainError
from .references import load_dataset, metric_statistics, particle_from_bean, sampled_particle

SOURCE_SCHEMA = "beanosort-simulation-source/v1"
SAMPLER_V1 = "population-weighted-sha256-resampling/v1"
SAMPLER = "population-weighted-sha256-resampling/v2"
INPUT_STREAM = "simulation_input"


def input_profile(session, operation_id):
    return session.scalar(select(Profile).filter_by(operation_id=operation_id, stream=INPUT_STREAM))


def _profile(session, portion):
    return session.scalar(select(Profile).filter_by(portion_id=portion.id).order_by(Profile.created_at.desc(), Profile.version.desc()))


def _material_population(session, portion, profile, dataset, beans, settings, seen):
    components = profile.data.get("material_components", []) if profile else []
    if not components:
        raise DomainError("This moved material has no retained population ancestry")
    resolved = [_resolve(session, session.get(Portion, item["portion_id"]), dataset, beans, settings, seen | {portion.id})
                for item in components]
    if len(components) == 1:
        result = resolved[0]
    else:
        stats = metric_statistics(beans, "area_mm2")
        by_sequence = {bean["bean_sequence"]: bean for bean in beans}
        weighted, total_count = {}, Fraction(0)
        for component, source in zip(components, resolved):
            parent_profile = session.get(Profile, component["profile_id"]) if component.get("profile_id") else None
            density = (parent_profile.data.get("count_per_kg") if parent_profile else None)
            model = source["mass_model"]
            weights = source["weights"]
            denominator = sum(count for _, count in weights)
            if not denominator:
                raise DomainError("A contributing population has no retained simulated beans")
            areas = [(by_sequence[seq].get(stats["field"]), count) for seq, count in weights]
            mean_area = (sum(Decimal(str(area))*count for area, count in areas)/denominator
                         if all(area is not None for area, _ in areas) else None)
            mean_mass = (Decimal(1000000)/Decimal(density) if density else Decimal(model["material_mean_mass_mg"])
                         if model.get("material_mean_mass_mg") else Decimal(model["reference_mean_mass_ug"])/1000)
            if not density and not model.get("material_mean_mass_mg") and mean_area and stats["mean"]:
                mean_mass *= mean_area/Decimal(str(stats["mean"]))
            count = Fraction(component["mass_mg"])/Fraction(mean_mass)
            total_count += count
            for seq, weight in weights:
                weighted[seq] = weighted.get(seq, Fraction(0)) + count*weight/denominator
        denominator = reduce(lcm, (value.denominator for value in weighted.values()), 1)
        integers = {seq: value.numerator*(denominator//value.denominator) for seq, value in weighted.items()}
        divisor = reduce(gcd, integers.values())
        model = deepcopy(resolved[0]["mass_model"])
        model["material_mean_mass_mg"] = str(Decimal(sum(item["mass_mg"] for item in components))*total_count.denominator/total_count.numerator)
        result = {"weights": [[seq, integers[seq]//divisor] for seq in sorted(integers)], "mass_model": model,
                  "ancestry": [entry for source in resolved for entry in source["ancestry"]]}
    result["membership_evidence"] = "inherited_estimate"
    result["ancestry"].append({"operation_id": portion.created_by, "portion_id": portion.id,
                              "basis": "representative measured contents movement; combined populations weighted by estimated bean count"})
    return result


def _resolve(session, portion, dataset, beans, settings, seen):
    if portion.id in seen or len(seen) > 100:
        raise DomainError("Source population ancestry is cyclic or exceeds the supported depth")
    operation = session.get(Operation, portion.created_by)
    if not operation:
        raise DomainError("Source material has no processing history")
    profile = _profile(session, portion)
    if profile and profile.data.get("material_population_unknown"):
        raise DomainError("This material's composition is unknown after an adjustment or non-representative movement")
    fallback = {"reference_mean_mass_ug": settings.mean_mass_ug if settings else 150000,
                "mass_assumption_settings_id": settings.id if settings else None}
    if operation.kind == "receiving":
        return {"weights": [[row["bean_sequence"], 1] for row in beans], "mass_model": fallback,
                "ancestry": [{"operation_id": operation.id, "portion_id": portion.id, "basis": "assumed original reference population"}],
                "membership_evidence": "inherited_estimate"}
    if operation.status != "completed":
        raise DomainError("Finish and release the source operation before simulating another pass")
    retained = profile.data.get("retained_population") if profile else None
    if retained:
        from .retained_populations import validate
        validate(retained)
        pools = retained["pools"]
        if len(pools) != 1 or pools[0]["dataset_id"] != dataset.id or pools[0]["dataset_fingerprint"] != dataset.fingerprint:
            raise DomainError("This retained population needs a compatible single reference dataset for simulation; its archived evidence remains viewable")
        return {"weights":deepcopy(pools[0]["weights"]), "mass_model":deepcopy(pools[0]["mass_model"]),
                "ancestry":[{"operation_id":operation.id,"portion_id":portion.id,"basis":"frozen retained joint population"}],
                "membership_evidence":"inherited_estimate"}
    if operation.kind == "contents_move":
        return _material_population(session, portion, profile, dataset, beans, settings, seen)
    combined = profile.data.get("combined_population") if profile else None
    if combined:
        if combined["dataset_id"] != dataset.id or combined["dataset_fingerprint"] != dataset.fingerprint:
            raise DomainError("Choose a recipe from the same reference dataset as the combined source")
        return {"weights":deepcopy(combined["weights"]),"mass_model":deepcopy(combined["mass_model"]),
                "ancestry":[{"operation_id":operation.id,"portion_id":portion.id,"basis":"count-weighted combined population estimate"}],
                "membership_evidence":"inherited_estimate"}
    if profile and profile.data.get("destination_combination"):
        raise DomainError("This combined population lacks compatible count/feature evidence for simulated replay")
    links = list(session.scalars(select(Input).filter_by(operation_id=operation.id)))
    if operation.kind == "sorting":
        from .collections import feed_input
        links = [feed_input(session, operation)]
    if len(links) != 1:
        raise DomainError("Simulation replay of combined populations is not implemented; use a retained single-source population")
    if operation.kind == "sorting":
        run = session.scalar(select(Run).filter_by(operation_id=operation.id))
        reference = run.source_versions.get("reference_population") if run else None
        if not reference or reference.get("dataset_id") != dataset.id or reference.get("fingerprint") != dataset.fingerprint:
            raise DomainError("Choose a recipe from the same reference dataset as the source population")
        output = session.scalar(select(Output).filter_by(operation_id=operation.id, portion_id=portion.id))
        from .segments import for_output,particle_filter
        collection=for_output(session,operation.id,output.stream) if output else None
        logical=collection.stream if collection else output.stream if output else None
        if not output or logical not in {"selected", "pass_through", "remainder"}:
            raise DomainError("This source has no supported simulated bulk output population")
        returning = bool(profile and profile.data.get("source_return"))
        if logical == "remainder" or returning:
            result = _resolve(session, session.get(Portion, links[0].portion_id), dataset, beans, settings, seen | {portion.id})
            result["membership_evidence"] = "inherited_estimate"
        else:
            seq = Particle.payload["reference_bean"]["bean_sequence"].as_integer()
            query=select(seq,func.count(Particle.id)).where(Particle.run_id==run.id,Particle.simulated_destination==logical)
            if collection:
                condition=particle_filter(session,collection)
                if condition is None:raise DomainError("This collection has no reliable particle association for simulated replay")
                query=query.where(condition)
            from .run_summaries import get_summary, membership_weights
            weights = membership_weights(get_summary(session, run.id, collection), logical)
            if weights is None:
                weights = [[key, count] for key, count in session.execute(query.group_by(seq).order_by(seq))]
            result = {"weights": weights, "mass_model": fallback, "ancestry": [], "membership_evidence": "simulation"}
        recon = session.scalar(select(Reconciliation).filter_by(operation_id=operation.id))
        model = recon.accounting.get("container_weighing", {}).get("model", {}) if recon else {}
        assumption = session.scalar(select(SimulationAssumption).filter_by(operation_id=operation.id))
        pinned = input_profile(session, operation.id)
        if model.get("mean_bean_mass_ug"):
            result["mass_model"] = {"reference_mean_mass_ug": model["mean_bean_mass_ug"],
                                    "mass_assumption_settings_id": model.get("mass_assumption_settings_id", model.get("settings_id"))}
        elif pinned:
            result["mass_model"] = deepcopy(pinned.data["mass_model"])
        elif assumption:
            result["mass_model"] = {"reference_mean_mass_ug": assumption.data["mean_mass_ug"],
                                    "mass_assumption_settings_id": assumption.settings_id}
        result["ancestry"].append({"operation_id": operation.id, "run_id": run.id,
                                   "portion_id": portion.id, "stream": output.stream,
                                   "basis": "inherited source population returned to the same vessel" if returning else "representative unprocessed remainder" if output.stream == "remainder" else "persisted simulated destinations"})
        return result
    if operation.kind == "split":
        profile = _profile(session, portion)
        if not profile or profile.data.get("representative_withdrawal_assumed") is not True:
            raise DomainError("A split needs an explicit representative-withdrawal assumption before population replay")
        result = _resolve(session, session.get(Portion, links[0].portion_id), dataset, beans, settings, seen | {portion.id})
        result["ancestry"].append({"operation_id": operation.id, "portion_id": portion.id,
                                   "basis": "representative split; weights are relative frequencies, not copied observed counts"})
        result["membership_evidence"] = "inherited_estimate"
        return result
    raise DomainError("This material has no supported retained simulation population; blend/pack/rework replay is not implemented")


def source_snapshot(session, portion, dataset, settings, planned_mg, *, force=False):
    """Pin a processed portion's own population; original receipts keep v1 sampling."""
    latest = _profile(session, portion)
    if latest and latest.data.get("material_population_unknown"):
        raise DomainError("This material's composition is unknown after an adjustment or non-representative movement")
    if session.get(Operation, portion.created_by).kind == "receiving" and not force:
        return None
    beans, _ = load_dataset(dataset)
    result = _resolve(session, portion, dataset, beans, settings, set())
    by_sequence = {row["bean_sequence"]: row for row in beans}
    weights = sorted(result["weights"])
    if not weights or any(type(seq) is not int or seq not in by_sequence or type(count) is not int or count <= 0 for seq, count in weights):
        raise DomainError("Source has mass but no usable simulated bean members; re-profile or repeat its source simulation with a larger test count")
    stats = metric_statistics(beans, "area_mm2")
    mean_area = Decimal(str(stats["mean"])) if stats["mean"] else None
    total = sum(count for _, count in weights)
    values = [(by_sequence[seq].get(stats["field"]), count) for seq, count in weights]
    pool_area = sum(Decimal(str(area))*count for area, count in values)/total if all(area is not None for area, _ in values) else None
    model = result["mass_model"]
    baseline = Decimal(model["reference_mean_mass_ug"])/1000
    profile = _profile(session, portion)
    density = Decimal(str(profile.data["count_per_kg"])) if profile and profile.data.get("count_per_kg") else None
    mean_mass = Decimal(1000000)/density if density and density.is_finite() and density > 0 else baseline*pool_area/mean_area if pool_area and mean_area else baseline
    if not density and model.get("material_mean_mass_mg"):
        mean_mass = Decimal(model["material_mean_mass_mg"])
    model.update(reference_mean_area_mm2=str(mean_area) if mean_area else None,
                 input_mean_bean_mass_mg=str(mean_mass), source_mean_area_mm2=str(pool_area) if pool_area else None,
                 input_mean_basis="retained weight/count estimate" if density else "source-weighted area/mass assumption" if pool_area and mean_area else "generic mean-mass assumption; area unknown")
    result.update(schema=SOURCE_SCHEMA, method=SAMPLER, dataset_id=dataset.id, dataset_fingerprint=dataset.fingerprint,
                  source_portion_id=portion.id, source_profile_id=profile.id if profile else None,
                  weights=weights, sampling_weight_total=total, reference_bean_count=len(weights),
                  physical_confirmation=False,
                  assumptions=["resample whole source bean records with replacement using retained relative multiplicity",
                               "weighed quantities do not establish an exact particle census",
                               "partial processing and complementary remainders assume representative withdrawals",
                               "new run identities do not establish permanent physical recognition"])
    if settings:
        from .simulation import estimate_particles
        basis = estimate_particles(planned_mg, settings)
        count = Decimal(planned_mg)/mean_mass
        basis.update(method="mass-over-retained-population-mean/v1", mean_mass_ug=str(mean_mass*1000),
                     reference_mean_mass_ug=model["reference_mean_mass_ug"], mean_mass_g=str(mean_mass/1000),
                     estimated_particles=str(count), rounded_particles=int(count.to_integral_value(rounding=ROUND_HALF_UP)),
                     source_portion_id=portion.id, source_profile_id=profile.id if profile else None,
                     mean_mass_evidence="inherited simulation estimate", source_mean_basis=model["input_mean_basis"])
        result["count_basis"] = basis
    result["fingerprint"] = digest(result)
    return result


def sampler_for_run(session, run, dataset, recipe, rows):
    key = (run.source_versions.get("reference_population") or {}).get("input_population_profile_id")
    if not key:
        return lambda sequence: sampled_particle(dataset, rows, recipe, run.id, run.seed, sequence)
    profile = session.get(Profile, key)
    if not profile or profile.operation_id != run.operation_id or profile.stream != INPUT_STREAM:
        raise DomainError("Pinned simulation source population is unavailable")
    source = profile.data
    if source.get("dataset_id") != dataset.id or source.get("dataset_fingerprint") != dataset.fingerprint or digest({k:v for k,v in source.items() if k != "fingerprint"}) != source.get("fingerprint"):
        raise DomainError("Pinned simulation source population does not match its immutable fingerprint")
    by_sequence = {row["bean_sequence"]: row for row in rows}
    cumulative, sequences, total = [], [], 0
    for sequence, weight in source["weights"]:
        total += weight; cumulative.append(total); sequences.append(sequence)
    def sample(sequence):
        method = source.get("method", SAMPLER_V1)
        hashed = int(digest([method, source["fingerprint"], run.id, run.seed, sequence]), 16)
        draw = hashed % total if method == SAMPLER_V1 else hashed*total//(2**256)
        bean = by_sequence[sequences[bisect_right(cumulative, draw)]]
        particle = particle_from_bean(dataset, bean, recipe, sequence)
        particle["reference_bean"].update(sampling=method, source_population_profile_id=key,
                                           source_portion_id=source["source_portion_id"])
        particle["evidence"]["features"].update(population_source=key,
            method="whole retained source-population bean replayed in a new synthetic passage")
        return particle
    return sample
