"""Frozen joint populations for material portions, independent of recipe pages.

Archive identities remain dataset-scoped. Combining count-weighted frequencies
never creates observations, photographs, stock, or a physical census.
"""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from functools import reduce
from math import gcd, lcm
from pathlib import Path

from sqlalchemy import func, select

from .auth import require_role
from .capture import digest
from .models import HandlingPlan, Operation, Portion, Profile, Reconciliation, ReferenceDataset, Run, User, identity
from .population_sources import source_snapshot
from .quantities import DomainError
from .references import METRICS, features, load_dataset, metric_statistics
from .service import audit, balance, get

SCHEMA = "beanosort-retained-population/v1"


class UnknownPopulation(DomainError):
    """A contributor lacks population evidence, rather than an archive being offline."""


def verified_dataset(session, dataset):
    root = Path(dataset.directory)
    try:
        signature = tuple((path.stat().st_mtime_ns, path.stat().st_size)
                          for path in (root/'beans.jsonl', root/'snapshot.json'))
    except OSError:
        return load_dataset(dataset, verify=True)
    cache = session.info.setdefault('verified_population_archives', {})
    key = dataset.id, dataset.fingerprint, signature
    if key not in cache:
        cache[key] = load_dataset(dataset, verify=True)
    return cache[key]


def validate(population):
    if population.get("schema") != SCHEMA or digest({key:value for key,value in population.items() if key != "fingerprint"}) != population.get("fingerprint"):
        raise DomainError("The retained population does not match its recorded fingerprint")


def _decimal(value):
    return Decimal(value.numerator) / value.denominator if isinstance(value, Fraction) else Decimal(value)


def _weights(weighted):
    denominator = reduce(lcm, (value.denominator for value in weighted.values()), 1)
    integers = {seq: value.numerator*(denominator//value.denominator) for seq, value in weighted.items() if value > 0}
    if not integers:
        raise DomainError("No retained archived bean members are available")
    divisor = reduce(gcd, integers.values())
    return [[seq, integers[seq]//divisor] for seq in sorted(integers)]


def _latest(session, pid):
    return session.scalar(select(Profile).where(Profile.portion_id == pid).order_by(Profile.created_at.desc(), Profile.version.desc()))


def _assemble(session, pid, data, quantity, seen, cache, original=None):
    if pid in seen or len(seen) >= 100:
        raise DomainError("Population ancestry is cyclic or exceeds the supported depth")
    if data.get("material_population_unknown"):
        raise UnknownPopulation("Composition is unknown after a non-representative movement or adjustment")
    retained = data.get("retained_population")
    if retained:
        validate(retained)
        ratio = quantity/Fraction(retained["net_mass_mg"])
        total = sum((Fraction(Decimal(pool["estimated_count"])) for pool in retained["pools"]), Fraction(0))
        count_ratio = quantity*Fraction(Decimal(data["count_per_kg"]))/1000000/total if data.get("count_per_kg") else ratio
        return [{**deepcopy(pool), "count": Fraction(Decimal(pool["estimated_count"]))*count_ratio,
                 "mass": Fraction(Decimal(pool["estimated_mass_mg"]))*ratio} for pool in retained["pools"]]
    components = data.get("material_components")
    if not components:
        portion = get(session, Portion, pid)
        operation = get(session, Operation, portion.created_by)
        if operation.kind in {"split", "blend"}:
            if operation.status != "completed":
                raise DomainError("Complete the source job before recovering its retained population")
            reconciliation = session.scalar(select(Reconciliation).filter_by(operation_id=operation.id))
            if operation.kind == "blend" and reconciliation and (reconciliation.discrepancy_mg or reconciliation.accounting.get("loss_mg")):
                raise DomainError("An earlier blend has unrepresented loss or discrepancy; exact retained membership cannot be reconstructed")
            plan = session.scalar(select(HandlingPlan).filter_by(operation_id=operation.id))
            if plan and (data.get("representative_withdrawal_assumed") or operation.kind == "blend"):
                components = plan.data.get("ingredients")
    if components:
        total = sum(component["mass_mg"] for component in components)
        if total <= 0:
            raise DomainError("Contributing masses are unavailable")
        groups = {}
        for component in components:
            parent = session.get(Profile, component.get("profile_id")) if component.get("profile_id") else _latest(session, component["portion_id"])
            values = _assemble(session, component["portion_id"], parent.data if parent else {},
                               quantity*component["mass_mg"]/total, seen | {pid}, cache, parent)
            for pool in values:
                key = (pool["dataset_id"], pool["dataset_fingerprint"])
                group = groups.setdefault(key, {"dataset_id": key[0], "dataset_fingerprint": key[1],
                    "mass_model": pool["mass_model"], "count": Fraction(0), "mass": Fraction(0), "weighted": {}})
                group["count"] += pool["count"]; group["mass"] += pool["mass"]
                denominator = sum(value for _, value in pool["weights"])
                for seq, value in pool["weights"]:
                    group["weighted"][seq] = group["weighted"].get(seq, Fraction(0)) + pool["count"]*value/denominator
        return [{**group, "weights": _weights(group["weighted"])} for group in groups.values()]
    portion = get(session, Portion, pid)
    operation = get(session, Operation, portion.created_by)
    run = session.scalar(select(Run).filter_by(operation_id=operation.id))
    combined = data.get("combined_population") or {}
    reference = (run.source_versions.get("reference_population") if run else data.get("reference_population")) or {}
    dataset_id = combined.get("dataset_id") or reference.get("dataset_id")
    if not dataset_id:
        raise UnknownPopulation("This contributing portion has no archived joint population evidence")
    dataset = get(session, ReferenceDataset, dataset_id)
    key = (pid, original.id if original else None, dataset.id)
    if key not in cache:
        cache[key] = source_snapshot(session, portion, dataset, None, 1000000, force=True)
    source = cache[key]
    density = data.get("count_per_kg")
    mean_mass = Fraction(1000000)/Fraction(Decimal(density)) if density else Fraction(Decimal(source["mass_model"]["input_mean_bean_mass_mg"]))
    return [{"dataset_id": dataset.id, "dataset_fingerprint": dataset.fingerprint,
             "weights": deepcopy(source["weights"]), "mass_model": deepcopy(source["mass_model"]),
             "count": quantity/mean_mass, "mass": quantity}]


def build_population(session, pid, data, net_mg, *, original=None):
    if net_mg <= 0:
        raise DomainError("A retained population needs positive contents mass")
    pools = _assemble(session, pid, data, Fraction(net_mg), set(), {}, original)
    result = {"schema": SCHEMA, "net_mass_mg": net_mg, "membership_evidence": "inherited_estimate",
              "physical_confirmation": False, "pools": []}
    total = Fraction(0)
    for pool in sorted(pools, key=lambda item: (item["dataset_id"], item["dataset_fingerprint"])):
        if pool["count"] <= 0 or pool["mass"] <= 0:
            raise DomainError("Contributing population count and mass estimates must be positive")
        dataset = get(session, ReferenceDataset, pool["dataset_id"])
        if dataset.fingerprint != pool["dataset_fingerprint"]:
            raise DomainError("The contributing reference fingerprint changed")
        rows, _ = verified_dataset(session, dataset)
        available = {row["bean_sequence"] for row in rows}
        if not pool["weights"] or any(seq not in available or type(weight) is not int or weight <= 0 for seq, weight in pool["weights"]):
            raise DomainError("A contributing archive no longer contains its recorded bean members")
        total += pool["count"]
        model = deepcopy(pool["mass_model"])
        model["material_mean_mass_mg"] = str(_decimal(pool["mass"]/pool["count"]))
        result["pools"].append({"dataset_id": dataset.id, "dataset_fingerprint": dataset.fingerprint,
            "weights": pool["weights"], "estimated_count": str(_decimal(pool["count"])),
            "estimated_mass_mg": str(_decimal(pool["mass"])), "mass_model": model})
    result["estimated_count"] = str(_decimal(total))
    result["fingerprint"] = digest(result)
    return result


def enrich_profile(session, data, pid, net_mg, *, original=None):
    result = deepcopy(data)
    if result.get("material_population_unknown") or not net_mg:
        return result
    density_evidence = result.get("count_density_evidence")
    try:
        population = build_population(session, pid, data, net_mg, original=original)
    except DomainError as exc:
        result["population_evidence_unavailable"] = str(exc)
        if isinstance(exc, UnknownPopulation) and len(data.get("material_components", [])) > 1:
            for key in ("retained_population", "population_analysis_url", "combined_population", "comparability_key"):
                result.pop(key, None)
            result.update(composition_evidence="unknown", distributions={}, count_per_kg=None, estimated_count=None,
                          note="Combined contents include material without known population evidence. The resulting population is unknown; contributing records remain in its ancestry.")
        return result
    result.pop("population_evidence_unavailable", None)
    result.update(retained_population=population, population_analysis_url=f"/portions/{pid}/populations",
                  composition_evidence="inherited_estimate", estimated_count=population["estimated_count"],
                  count_per_kg=str(Decimal(population["estimated_count"])*1000000/net_mg),
                  count_density_evidence=density_evidence if isinstance(density_evidence, dict) else "inherited archived population and recorded contribution masses",
                  note="Joint archived features and source photographs are retained. Counts and distributions are inherited estimates weighted by source count densities and recorded contribution masses; physical membership and mixing uniformity are not established.")
    report = population_report(session, population)
    target = report["populations"]["input"]; total = Decimal(target["count"])
    result["histogram_definitions"] = report["definitions"]
    result["distributions"] = {key: {**{f"[{spec['edges'][i]}, {spec['edges'][i+1]})":str(Decimal(value)/total)
        for i, value in enumerate(target["histograms"][key])}, "unknown":str(Decimal(target["unknown"][key])/total)}
        for key, spec in report["definitions"].items()}
    result["comparability_key"] = "archived-joint-features:" + ":".join(pool["dataset_fingerprint"] for pool in population["pools"])
    return result


def members(session, population):
    validate(population)
    result = []
    for pool in population["pools"]:
        dataset = get(session, ReferenceDataset, pool["dataset_id"])
        if dataset.fingerprint != pool["dataset_fingerprint"]:
            raise DomainError("The contributing reference fingerprint changed")
        rows, snapshot = load_dataset(dataset)
        by_sequence = {row["bean_sequence"]: row for row in rows}
        definitions = {**METRICS, **dataset.metadata_json["metrics"]}
        total = sum(weight for _, weight in pool["weights"])
        for seq, weight in pool["weights"]:
            if seq not in by_sequence:
                raise DomainError("A contributing archived bean is unavailable")
            result.append({"dataset": dataset, "bean": by_sequence[seq], "snapshot": snapshot,
                           "features": features(by_sequence[seq], definitions),
                           "count": Fraction(Decimal(pool["estimated_count"]))*weight/total})
    return result


def population_report(session, population):
    rows = members(session, population)
    # Archive-specific field names are read before deriving shared display bins.
    feature_rows = [{spec["field"]: row["features"].get(key) for key, spec in METRICS.items()} for row in rows]
    definitions = {key: metric_statistics(feature_rows, key) for key in METRICS}
    histograms = {key: [Fraction(0)]*(len(spec["edges"])-1) for key, spec in definitions.items()}
    unknown = {key: Fraction(0) for key in definitions}
    for row in rows:
        for key, spec in definitions.items():
            value = row["features"][key]
            bucket = next((i for i,(lo,hi) in enumerate(zip(spec["edges"], spec["edges"][1:])) if value is not None and lo <= value < hi), None)
            if bucket is None: unknown[key] += row["count"]
            else: histograms[key][bucket] += row["count"]
    return {"schema": "beanosort-portion-populations/v1", "single_population": True,
            "series_titles": {"input": "Current contents"}, "recipe": {"feature": None, "relation": "pass_all"},
            "definitions": definitions, "physical_confirmation": False,
            "definition_statistics_basis": "Distinct archived records define display bins; histogram frequencies use estimated bean-count weights.",
            "basis": "Retained joint archive records, weighted by estimated bean count and measured material contributions; not a physical census.",
            "populations": {"input": {"count": population["estimated_count"],
                "histograms": {key: [str(_decimal(value)) for value in values] for key, values in histograms.items()},
                "unknown": {key: str(_decimal(value)) for key,value in unknown.items()}}}}


def inspect_population(session, population, *, feature, bucket, page=1):
    from .population_inspection import PAGE_SIZE, colour_samples
    report = population_report(session, population)
    if feature not in report["definitions"] or type(bucket) is not int or type(page) is not int or not 1 <= page <= 100000:
        raise DomainError("Choose a valid chart interval and page")
    spec = report["definitions"][feature]
    if not 0 <= bucket < len(spec["edges"])-1:
        raise DomainError("Choose a valid chart interval")
    lo, hi = spec["edges"][bucket:bucket+2]
    chosen = [row for row in members(session, population) if row["features"][feature] is not None and lo <= row["features"][feature] < hi]
    chosen.sort(key=lambda row:(row["dataset"].id,row["bean"]["bean_sequence"]))
    total = sum((row["count"] for row in chosen), Fraction(0)); pages = max(1,(len(chosen)+PAGE_SIZE-1)//PAGE_SIZE); page = min(page,pages)
    items=[]
    for row in chosen[(page-1)*PAGE_SIZE:page*PAGE_SIZE]:
        bean=row["bean"];dataset=row["dataset"]
        items.append({"dataset_id":dataset.id,"bean_sequence":bean["bean_sequence"],"bean_id":bean["bean_id"],
            "feature_value":row["features"][feature],"count":str(_decimal(row["count"])),"recorded_passages":0,"missed_target_passages":0,
            "colours":colour_samples(bean),"photos":{camera:f"/reference-datasets/{dataset.id}/photos/{bean['bean_sequence']}/{camera}"
                for camera in ("CamL","CamR") if bean.get("photo_"+camera) in row["snapshot"]["photo_hashes"]}})
    return {"schema":"beanosort-population-inspection/v1","feature":feature,"title":spec["title"],"unit":spec["unit"],
        "bucket":bucket,"lower":lo,"upper":hi,"interval":"[lower,upper)","population":"input","basis":"expected",
        "matching_count":str(_decimal(total)),"reference_bean_count":len(chosen),"page":page,"pages":pages,"page_size":PAGE_SIZE,
        "physical_confirmation":False,"collection":True,"retained":True,"run_id":None,"count_unit":"estimated beans",
        "count_basis":"estimated beans","photo_evidence":"archived source images, not physical membership verification",
        "membership_evidence":"inherited_estimate","items":items}


def recover_profiles(db, user_id, portion_ids, reason):
    if not reason.strip():
        raise DomainError("Record the population-recovery reason")
    result=[]
    with db.write() as session:
        user=get(session,User,user_id);require_role(user,"supervisor","admin")
        for pid in portion_ids:
            portion=get(session,Portion,pid);original=_latest(session,pid)
            if not original:raise DomainError("This portion has no profile to recover")
            existing=original.data.get("retained_population")
            if existing and existing["net_mass_mg"]==balance(session,pid) and original.data.get("population_analysis_url")==f"/portions/{pid}/populations":
                validate(existing)
                result.append({"portion_id":pid,"profile_id":original.id,"already_recovered":True});continue
            updated=enrich_profile(session,original.data,pid,balance(session,pid),original=original)
            population=updated.get("retained_population")
            if not population:raise DomainError(updated.get("population_evidence_unavailable","The population cannot be recovered without inventing membership"))
            if (original.data.get("retained_population") or {}).get("fingerprint")==population["fingerprint"]:
                result.append({"portion_id":pid,"profile_id":original.id,"already_recovered":True});continue
            version=(session.scalar(select(func.max(Profile.version)).filter_by(operation_id=portion.created_by,stream="population_recovery")) or 0)+1
            profile=Profile(id=identity("profile"),operation_id=portion.created_by,portion_id=pid,stream="population_recovery",version=version,data=updated)
            session.add(profile);session.flush()
            audit(session,user,"population_evidence_recovered",{"portion_id":pid,"previous_profile_id":original.id,
                "recovered_profile_id":profile.id,"fingerprint":population["fingerprint"],"reason":reason,
                "stock_changes":False},get(session,Operation,portion.created_by))
            result.append({"portion_id":pid,"profile_id":profile.id,"estimated_count":population["estimated_count"],
                           "reference_members":sum(len(pool["weights"]) for pool in population["pools"])})
    return result
