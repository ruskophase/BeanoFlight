"""Read-only statistics snapshots and empirical simulated populations.

No tracker, image processing, live sorter imports or hardware interfaces. A sampled
row retains all joint properties and photo references from one archived bean.
"""
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import tempfile
from urllib.parse import urlparse

from sqlalchemy import func, select

from .auth import require_role
from .capture import FEATURE_SCHEMA, digest, probability
from .models import Audit, ReferenceDataset, User, Version, identity
from .quantities import DomainError

RECIPE_SCHEMA = "beanosort-reference-recipe/v1"
SAMPLER = "whole-bean-sha256-resampling/v1"
DEFAULT_RECIPE = "Dark outliers: L* below median minus 2 sigma"
METRICS = {
    "lab_l": {"title": "Lightness", "unit": "CIE L*", "field": "combined_lab_l_mean", "step": 1, "upper": 101},
    "lab_chroma": {"title": "Chroma", "unit": "CIE chroma", "field": "combined_lab_chroma_mean", "step": 1, "upper": 1000},
    "size_mm": {"title": "Major axis (CamL)", "unit": "mm", "field": "caml_ellipse_major_mm_median", "step": 1, "upper": 1000},
    "volume_proxy_mm3": {"title": "Ellipsoid volume proxy", "unit": "mm³ proxy, not mass", "field": "rotational_ellipsoid_volume_proxy_mm3_median", "step": 25, "upper": 1000000},
    "aspect_ratio": {"title": "Aspect ratio (CamL)", "unit": "ratio", "field": "caml_ellipse_aspect_ratio_median", "step": .2, "upper": 1000},
    "area_mm2": {"title": "Projected area (two views)", "unit": "mm²", "field": "projected_area_geomean_mm2_median", "step": 5, "upper": 1000000},
}


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(text):
    try:
        return json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError) as exc:
        raise DomainError("Malformed/nonfinite reference JSON") from exc


def inside(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise DomainError("Reference file must have a relative path")
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise DomainError("Reference file escapes its snapshot directory")
    return path


def features(row, definitions=None):
    result = {"shape": "unknown", "defects": None, "brightness": None}
    for key, spec in (METRICS if definitions is None else definitions).items():
        value = row.get(spec["field"])
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or
                                  not math.isfinite(value) or not 0 <= value < spec["upper"]):
            raise DomainError(f"Invalid reference feature: {spec['field']}")
        result[key] = value
    return result


def metric_statistics(rows, key):
    spec = METRICS[key]
    values = [features(row, {key: spec})[key] for row in rows if row.get(spec["field"]) is not None]
    entry = {**spec, "count": len(values), "unknown": len(rows) - len(values),
             "method": "one aggregate per tracked bean; population SD about arithmetic mean; median centre"}
    if values:
        entry.update(median=statistics.median(values), mean=statistics.mean(values),
                     sigma=statistics.pstdev(values), minimum=min(values), maximum=max(values))
        step = spec["step"]
        lower = math.floor(min(values) / step) * step
        bins = min(100, max(1, math.floor((max(values) - lower) / step) + 1))
        width = max(step, math.ceil((max(values) - lower + step) / 100 / step) * step)
        entry["edges"] = [round(lower + i * width, 8) for i in range(bins + 1)]
    else:
        entry.update(median=None, mean=None, sigma=None, minimum=None, maximum=None, edges=[0, spec["step"]])
    return entry


def summarize(rows):
    return {key: metric_statistics(rows, key) for key in METRICS}


def recipe_definitions(dataset, recipe):
    # Old snapshots and runs keep their original feature set. New recipe metrics
    # are added explicitly, without rewriting a registered snapshot or old payload.
    definitions = dict(dataset.metadata_json["metrics"])
    if recipe["feature"] not in definitions:
        definitions[recipe["feature"]] = recipe["statistics"]
    return definitions


def import_bundle(db, user_id, directory, storage, *, name, source_url=""):
    """Copy verified bean records and representative photos; never modify source."""
    from .service import audit, get
    with db.read() as s:
        require_role(get(s, User, user_id), "admin")
    if source_url and (urlparse(source_url).scheme not in {"http", "https"} or not urlparse(source_url).netloc):
        raise DomainError("Reference source URL must be an absolute HTTP(S) URL")
    source = Path(directory).resolve()
    raw_manifest = (source / "manifest.json").read_bytes()
    manifest = read_json(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("schema") != "beanoflight-statistics-bundle/v1":
        raise DomainError("Expected a beanoflight-statistics-bundle/v1 reference")
    entries = {entry["path"]: entry for entry in manifest.get("files", [])}
    beans = source / "beans.jsonl"
    raw_beans = beans.read_bytes()
    bean_hash = hashlib.sha256(raw_beans).hexdigest()
    if entries.get("beans.jsonl", {}).get("sha256") != bean_hash:
        raise DomainError("Reference beans.jsonl hash does not match its manifest")
    rows = [read_json(line) for line in raw_beans.splitlines() if line.strip()]
    if not rows or len(rows) > 100000 or any(not isinstance(row,dict) or row.get("schema") != "beanoflight-statistics-bean/v1" for row in rows):
        raise DomainError("Reference requires 1–100000 aggregate bean records with the known schema")
    if len({row.get("bean_id") for row in rows}) != len(rows) or any(not isinstance(row.get("bean_id"), str) for row in rows):
        raise DomainError("Reference bean identities must be unique")
    if any(type(row.get("bean_sequence")) is not int for row in rows) or len({r['bean_sequence'] for r in rows}) != len(rows):
        raise DomainError("Reference requires unique numeric bean sequences")
    for row in rows:
        features(row)
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    fingerprint = digest([manifest_hash, bean_hash, "reference-snapshot/v1"])
    key = "dataset-" + fingerprint[:48]
    root = Path(storage).resolve() / fingerprint
    with db.read() as s:
        existing = s.get(ReferenceDataset, key)
        if existing:
            load_dataset(existing, verify=True)
            return key
    photo_names = {row.get("photo_" + camera) for row in rows for camera in ("CamL", "CamR")} - {None, ""}
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reference-", dir=root.parent) as staging:
        temporary = Path(staging)
        photo_hashes = {}
        for photo in sorted(photo_names):
            original = inside(source, photo)
            entry = entries.get(photo)
            if not photo.startswith("photos/") or original.suffix.lower() not in {".jpg", ".jpeg", ".png"} or not entry or sha256(original) != entry.get("sha256"):
                raise DomainError("Missing, unsafe or changed reference photograph")
            target = inside(temporary, photo)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            if sha256(target) != entry["sha256"]:
                raise DomainError("Reference photograph changed while it was copied")
            photo_hashes[photo] = entry["sha256"]
        (temporary / "beans.jsonl").write_bytes(raw_beans)
        (temporary / "source-manifest.json").write_bytes(raw_manifest)
        snapshot = {"schema": "beanosort-reference-snapshot/v1", "beans_sha256": bean_hash,
                    "photo_hashes": photo_hashes, "source_manifest_sha256": manifest_hash}
        (temporary / "snapshot.json").write_text(json.dumps(snapshot, sort_keys=True, allow_nan=False))
        metadata = {"bean_count": len(rows), "sample_count": sum(row.get("sample_count", 0) for row in rows),
                    "representative_photo_count": len(photo_hashes), "original_photo_count": manifest.get("summary", {}).get("bean_photos", {}).get("image_count"),
                    "beans_sha256": bean_hash, "snapshot_sha256": sha256(temporary / "snapshot.json"),
                    "source_manifest_sha256": manifest_hash, "source_url": source_url,
                    "source_counts": manifest.get("summary", {}).get("counts", {}),
                    "source_recording": manifest.get("recording"), "source_provenance": manifest.get("provenance", {}),
                    "definitions": manifest.get("definitions", {}), "metrics": summarize(rows),
                    "physical_coverage": "unknown; tracked subset, not a physical census"}
        if root.exists():
            if sha256(root / "snapshot.json") != metadata["snapshot_sha256"] or sha256(root / "beans.jsonl") != bean_hash:
                raise DomainError("Existing snapshot differs; choose a clean reference storage directory")
        else:
            temporary.rename(root)
    with db.write() as s:
        user = get(s, User, user_id)
        require_role(user, "admin")
        if not s.get(ReferenceDataset, key):
            s.add(ReferenceDataset(id=key, name=str(name).strip()[:120] or "Bean reference", fingerprint=fingerprint,
                                  directory=str(root), metadata_json=metadata, operator_id=user.id))
            audit(s, user, "reference_dataset_imported", {"dataset_id": key, "fingerprint": fingerprint,
                  "bean_count": len(rows), "source_url": source_url, "method": "verified read-only source snapshot"})
    return key


@lru_cache(maxsize=8)
def _load(directory, beans_hash, snapshot_hash, beans_stat, snapshot_stat):
    root = Path(directory)
    if sha256(root / "beans.jsonl") != beans_hash or sha256(root / "snapshot.json") != snapshot_hash:
        raise DomainError("Reference snapshot changed; restore the pinned evidence files")
    return ([read_json(line) for line in (root / "beans.jsonl").read_text().splitlines() if line.strip()],
            read_json((root / "snapshot.json").read_text()))


def load_dataset(dataset, *, verify=False):
    root, meta = Path(dataset.directory), dataset.metadata_json
    try:
        if verify and (sha256(root / "beans.jsonl") != meta["beans_sha256"] or sha256(root / "snapshot.json") != meta["snapshot_sha256"]):
            raise DomainError("Reference snapshot changed; restore the pinned evidence files")
        return _load(str(root), meta["beans_sha256"], meta["snapshot_sha256"],
                     (root / "beans.jsonl").stat().st_mtime_ns, (root / "snapshot.json").stat().st_mtime_ns)
    except OSError as exc:
        raise DomainError("Pinned reference files are unavailable; restore their snapshot") from exc


def recipe_data(dataset, feature="lab_l", relation="below", sigma="2"):
    if relation == "pass_all":
        if feature != "area_mm2":
            raise DomainError("Hopper emptying uses the reference area schema without a selection threshold")
        stats = dataset.metadata_json["metrics"].get(feature)
        if stats is None:
            rows, _ = load_dataset(dataset)
            stats = metric_statistics(rows, feature)
        return {"schema": RECIPE_SCHEMA, "execution_scope": "simulation_only",
                "operation_mode": "hopper_emptying", "dataset_id": dataset.id,
                "dataset_fingerprint": dataset.fingerprint, "feature": feature,
                "relation": "pass_all", "statistics": stats, "method": SAMPLER,
                "missing_feature": "intentionally passed; feature remains unknown",
                "rule": "All beans intentionally pass; no selection decisions or selection actions",
                "assumptions": "Archived whole-bean features simulate passages; no hardware commands are sent"}
    if feature not in METRICS or relation not in {"below", "above", "within", "outside"}:
        raise DomainError("Choose a supported feature and comparison")
    try:
        multiplier = Decimal(str(sigma))
        if not multiplier.is_finite() or not 0 < multiplier <= 10:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise DomainError("Standard-deviation multiplier must be greater than zero and at most 10") from None
    stats = dataset.metadata_json["metrics"].get(feature)
    if stats is None:
        rows, _ = load_dataset(dataset)
        stats = metric_statistics(rows, feature)
    if not stats["count"] or stats["sigma"] is None:
        raise DomainError("Reference has no known values for this feature")
    median, sd = Decimal(str(stats["median"])), Decimal(str(stats["sigma"]))
    return {"schema": RECIPE_SCHEMA, "execution_scope": "simulation_only", "dataset_id": dataset.id,
            "dataset_fingerprint": dataset.fingerprint, "feature": feature, "relation": relation, "sigma": str(multiplier),
            "lower": str(median - multiplier * sd), "upper": str(median + multiplier * sd),
            "statistics": stats, "statistics_basis": "all distinct reference beans with known feature; population SD (ddof=0)",
            "comparison": "strict", "missing_feature": "unresolved; no selection attempt",
            "method": SAMPLER, "assumptions": "whole measured bean records sampled uniformly with replacement; synthetic new passages; every selection issues an intended action"}


def validate_recipe(session, data):
    from .service import get
    dataset = get(session, ReferenceDataset, data.get("dataset_id"))
    expected = recipe_data(dataset, data.get("feature"), data.get("relation"), data.get("sigma"))
    if data != expected:
        raise DomainError("Reference recipe must pin the exact dataset statistics and thresholds")
    return dataset


def create_recipe(db, user_id, key, name, dataset_id, feature="lab_l", relation="below", sigma="2"):
    from .service import audit, get, remember, replay
    name = str(name or "").strip()
    if not 1 <= len(name) <= 120:
        raise DomainError("Recipe name is required (up to 120 characters)")
    payload = {"command": "create_reference_recipe", "operator": user_id, "name": name,
               "dataset_id": dataset_id, "feature": feature, "relation": relation, "sigma": str(sigma)}
    with db.write() as s:
        user = get(s, User, user_id)
        require_role(user, "admin")
        previous = replay(s, key, payload)
        if previous:
            return previous
        dataset = get(s, ReferenceDataset, dataset_id)
        data = recipe_data(dataset, feature, relation, sigma)
        number = (s.scalar(select(func.max(Version.version)).filter_by(kind="recipe", name=name)) or 0) + 1
        recipe = Version(id=identity("version"), kind="recipe", name=name, version=number, data=data, operator_id=user.id)
        s.add(recipe)
        audit(s, user, "version_created", {"version_id": recipe.id, "kind": "recipe", "version": number,
              "dataset_id": dataset.id, "rule": data})
        remember(s, key, payload, recipe.id)
        return recipe.id


def matches(value, recipe):
    if recipe.get("relation") == "pass_all":
        return False
    if value is None:
        return None
    value = Decimal(str(value))
    lower, upper = Decimal(recipe["lower"]), Decimal(recipe["upper"])
    return {"below": value < lower, "above": value > upper, "within": lower < value < upper,
            "outside": value < lower or value > upper}[recipe["relation"]]


def retired_recipe_ids(session):
    return {data["version_id"] for data in session.scalars(select(Audit.data).filter_by(action="recipe_retired"))}


def retire_recipe(db, user_id, key, recipe_id, reason):
    from .service import audit, get, remember, replay
    payload = {"command": "retire_recipe", "operator": user_id, "recipe_id": recipe_id, "reason": reason}
    with db.write() as s:
        user = get(s, User, user_id)
        require_role(user, "admin")
        previous = replay(s, key, payload)
        if previous: return previous
        recipe = get(s, Version, recipe_id)
        if recipe.kind != "recipe" or not isinstance(reason, str) or not reason.strip():
            raise DomainError("Choose a recipe and record its retirement reason")
        if recipe_id not in retired_recipe_ids(s):
            audit(s, user, "recipe_retired", {"version_id": recipe_id, "name": recipe.name,
                  "version": recipe.version, "reason": reason, "existing_jobs": "keep their pinned version"})
        remember(s, key, payload, recipe_id)
        return recipe_id


def sampled_particle(dataset, rows, recipe, run_id, seed, sequence):
    index = int(digest([SAMPLER, dataset.fingerprint, run_id, str(seed), sequence]), 16) % len(rows)
    bean = rows[index]
    return particle_from_bean(dataset, bean, recipe, sequence)


def particle_from_bean(dataset, bean, recipe, sequence):
    """Existing feature/decision logic shared by initial and subsequent passes."""
    values = features(bean, recipe_definitions(dataset, recipe))
    selected = matches(values[recipe["feature"]], recipe)
    return {"schema": FEATURE_SCHEMA, "key": str(sequence), "sequence": sequence, "source_at": None,
            "decision": "unknown" if selected is None else "selected" if selected else "pass",
            "action": "attempted" if selected else "not_issued", "features": values,
            "frames_observed": bean.get("sample_count"), "valve_count": None,
            "reference_bean": {"dataset_id": dataset.id, "fingerprint": dataset.fingerprint,
                               "bean_id": bean["bean_id"], "bean_sequence": bean["bean_sequence"],
                               "original_feature_evidence": "image_derived", "sampling": SAMPLER},
            "evidence": {"features": {"kind": "simulation", "source": dataset.id, "version": dataset.fingerprint,
                                      "origin_kind": "image_derived", "method": "whole measured reference row replayed in a synthetic population"},
                         "decision": {"kind": "simulation", "source": "reference recipe", "version": digest(recipe)},
                         "action": {"kind": "simulation", "source": "assumed intended selection action", "version": "all-decisions-issued/v1"}}}


def population_report(items, dataset, recipe, model):
    """Aligned additive count histograms; real-source features, synthetic membership."""
    p = probability(model)
    populations = {key: {"count": Decimal(0), "histograms": {}, "unknown": {}} for key in
                   ("input", "targeted", "expected_selected", "expected_pass_through", "unresolved", "simulated_selected", "simulated_pass_through")}
    definitions = recipe_definitions(dataset, recipe)
    for pop in populations.values():
        for feature, spec in definitions.items():
            pop["histograms"][feature] = [Decimal(0)] * (len(spec["edges"]) - 1)
            pop["unknown"][feature] = Decimal(0)
    for item in items:
        row = item["payload"]
        targeted = row["decision"] == "selected" and row["action"] == "attempted"
        weights = {"input": Decimal(1), "targeted": Decimal(int(targeted))}
        if targeted:
            weights.update(expected_selected=p, expected_pass_through=1-p)
        elif row["decision"] == "pass":
            weights["expected_pass_through"] = Decimal(1)
        else:
            weights["unresolved"] = Decimal(1)
        if item.get("simulated_destination"):
            weights["simulated_"+item["simulated_destination"]] = Decimal(1)
        for name, weight in weights.items():
            populations[name]["count"] += weight
            for feature, spec in definitions.items():
                value = row["features"].get(feature)
                bucket = next((i for i,(lo,hi) in enumerate(zip(spec['edges'],spec['edges'][1:])) if value is not None and lo <= value < hi),None)
                if bucket is None:
                    populations[name]["unknown"][feature] += weight
                else:
                    populations[name]["histograms"][feature][bucket] += weight
    def plain(value):
        if isinstance(value, Decimal): return str(value)
        if isinstance(value, dict): return {k:plain(v) for k,v in value.items()}
        if isinstance(value, list): return [plain(v) for v in value]
        return value
    return {"schema": "beanosort-reference-populations/v1", "dataset_id": dataset.id,
            "dataset_fingerprint": dataset.fingerprint, "definitions": definitions, "recipe": recipe,
            "capture_model": model,
            "basis": "tracked simulated passages; count weighting; bins [lower,upper); unknown separate",
            "physical_confirmation": False, "populations": plain(populations)}


def photo_path(dataset, bean_sequence, camera):
    if camera not in {"CamL", "CamR"}:
        raise DomainError("Unknown reference camera")
    rows, snapshot = load_dataset(dataset)
    row = next((r for r in rows if r["bean_sequence"] == bean_sequence), None)
    name = row.get("photo_"+camera) if row else None
    if not name or name not in snapshot['photo_hashes']:
        raise DomainError("Reference photograph is unavailable")
    path = inside(dataset.directory, name)
    if sha256(path) != snapshot['photo_hashes'][name]:
        raise DomainError("Reference photograph changed; restore its snapshot")
    return path
