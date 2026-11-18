"""Offline, read-only BeanRegistry v3 adapter and durable, replayable batch spool.

No sorter imports, sockets, cameras or device commands. Unknown decisions/actions
stay unknown; replay and virtual actuator records never prove real issuance.
"""
from collections import Counter
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from sqlalchemy import select

from .auth import require_role
from .capture import FEATURE_SCHEMA, digest, validate_particle
from .models import Operation, Run, SourceBundle, User, identity
from .quantities import DomainError
from .service import audit, get

ADAPTER_VERSION = "beanoregistry-offline/v1"
PHYSICAL_ACTUATORS = {"esp32-s2-gptimer"}
FEATURE_FIELDS = {
    "volume_proxy_mm3": ("rotational_ellipsoid_volume_proxy_mm3_median",),
    "lab_l": ("caml_lab_l_mean_median", "caml_approx_lab_l_median"),
    "lab_chroma": ("caml_lab_chroma_mean_median", "caml_approx_lab_chroma_median"),
    "size_mm": ("caml_ellipse_major_mm_median",),
}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda x: (_ for _ in ()).throw(DomainError(f"Nonfinite JSON value: {x}")))


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def bundle_features(directory, run_id):
    """Only join exports with a full, matching source run ID. No prefix guessing."""
    directory = Path(directory).resolve()
    manifest = load_json(directory / "manifest.json")
    declared_run = manifest.get("summary", {}).get("source_run_id") or manifest.get("run_id")
    if declared_run != run_id:
        raise DomainError("Statistics bundle lacks the exact matching source run identity; cannot join by a shortened ID")
    if manifest.get("schema") not in {"beanoflight-live-statistics-bundle/v1", "beanoflight-statistics-bundle/v1"}:
        raise DomainError("Unsupported statistics bundle schema")
    files = {x["path"]: x for x in manifest.get("files", [])}
    entry = files.get("beans.jsonl")
    path = directory / "beans.jsonl"
    if not entry or entry.get("sha256") != sha256(path):
        raise DomainError("Statistics bean file hash missing or invalid")
    rows = {}
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            sequence = row.get("bean_sequence")
            if type(sequence) is not int or sequence in rows:
                raise DomainError("Statistics rows need unique numeric bean sequences")
            rows[sequence] = row
    return rows, {"path": str(directory), "manifest_sha256": sha256(directory / "manifest.json"),
                  "beans_sha256": entry["sha256"], "provenance": manifest.get("provenance", {}),
                  "definitions": manifest.get("definitions", {}),
                  "schema": manifest["schema"]}


def export_registry(database, run_id, output, *, statistics=None):
    database, output = Path(database).resolve(), Path(output).resolve()
    if output.exists():
        raise DomainError("Export destination already exists; reuse its spool or choose a new directory")
    if not database.is_file():
        raise DomainError("Registry file does not exist")
    features, statistics_ref = bundle_features(statistics, run_id) if statistics else ({}, None)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        if c.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise DomainError("Only BeanRegistry schema 3 is supported")
        session = c.execute("SELECT * FROM sessions WHERE run_id=?", (run_id,)).fetchone()
        if session is None or session["status"] not in {"completed", "failed"}:
            raise DomainError("Choose a completed/failed archived run; active runs are not imported")
        session = dict(session)
        settings = json.loads(session.pop("settings_json"))
        live = session["source_kind"].startswith("live-")
        if not live:
            raise DomainError("This adapter supports archived live-source runs only; replay provenance needs a separate reviewed adapter")
        environment = "real" if live else "simulation"
        decisions = {}
        for row in c.execute("SELECT * FROM sorting_decisions WHERE run_id=? ORDER BY registry_revision", (run_id,)):
            if row["sequence"] in decisions:
                raise DomainError("Multiple decisions for one particle require an explicit segment adapter")
            decisions[row["sequence"]] = dict(row)
        actions = {}
        for r in c.execute("SELECT * FROM actuation_results WHERE run_id=?", (run_id,)):
            if r["sequence"] in actions:
                raise DomainError("Multiple action results for one particle require an explicit segment adapter")
            actions[r["sequence"]] = dict(r)
        enrichments = {}
        for row in c.execute("SELECT * FROM enrichments WHERE run_id=? AND kind='classification_decision_basis'", (run_id,)):
            if row["sequence"] in enrichments:
                raise DomainError("Multiple classification decision bases require review")
            enrichments[row["sequence"]] = dict(row)
        classifier_members = {r["result_id"]: dict(r) for r in c.execute(
            "SELECT result_id,source,model_version FROM enrichments WHERE run_id=? AND kind='classification_evidence'", (run_id,))}
        beans = [dict(row) for row in c.execute("SELECT * FROM beans WHERE run_id=? ORDER BY sequence", (run_id,))]
        cursor = c.execute("SELECT COALESCE(MAX(event_sequence),0) FROM registry_events").fetchone()[0]
        c.rollback()
    if not beans:
        raise DomainError("Run contains no tracked particles")
    unknown_features = set(features) - {row["sequence"] for row in beans}
    if unknown_features:
        raise DomainError("Statistics contain particles absent from the chosen Registry run")
    rows, counts = [], Counter()
    for ordinal, bean in enumerate(beans):
        sequence = bean["sequence"]
        decision, action, basis = decisions.get(sequence), actions.get(sequence), enrichments.get(sequence)
        selected, issued = "unknown", "unknown"
        decision_kind, action_kind = "unknown", "unknown"
        if decision:
            decision_kind = "controller_record" if live else "simulation"
            if json.loads(decision["gate_indices_json"]):
                selected = "selected"
            elif decision["source"] == "beano-sorter" and decision["reason"].startswith(("category accepted", "reject category")):
                selected, issued = "pass", "not_issued"
            elif decision["source"] == "beano-sorter" and decision["reason"].startswith("no gate reached"):
                selected, issued = "selected", "not_issued"
        if action:
            if not decision or action["decision_id"] != decision["decision_id"]:
                raise DomainError("Action does not match the particle's decision")
            physical = action["source"] in PHYSICAL_ACTUATORS
            action_kind = "controller_record" if live and physical else "simulation" if "virtual" in action["source"] else "unknown"
            if action["success"] and action["actual_close_timestamp_ns"] > action["actual_open_timestamp_ns"] and selected == "selected":
                issued = "attempted" if (physical or not live) else "unknown"
            # A failed gate-cycle coverage check does not prove no pulse occurred.
        raw_features = features.get(sequence, {})
        compact = {"shape": None, "brightness": None, "defects": None}
        feature_mapping = {}
        for key, names in FEATURE_FIELDS.items():
            field = next((name for name in names if raw_features.get(name) is not None), None)
            compact[key] = raw_features[field] if field else None
            feature_mapping[key] = field
        members = []
        synthetic_classification = False
        if basis:
            classification = json.loads(basis["value_json"])
            members = [classifier_members.get(key, {"result_id": key, "source": "unknown", "model_version": "unknown"})
                       for key in classification.get("ensemble", {}).get("member_result_ids", [])]
            model_versions = [basis["model_version"]] + [m["model_version"] for m in members]
            synthetic_classification = any(str(v).startswith("mock-") for v in model_versions)
            # Preserve the classifier result, without inventing a defect ontology.
            # Known mock categories remain in source evidence, not real population claims.
            compact["classification_category"] = None if synthetic_classification else classification.get("category")
        evidence = {
            "features": {"kind": "image_derived" if live and raw_features else "unknown" if live else "simulation", "source": "statistics-bundle" if raw_features else "unavailable", "version": statistics_ref["schema"] if statistics_ref else "unknown"},
            "decision": {"kind": decision_kind if live else "simulation", "source": decision["source"] if decision else "unavailable", "version": decision["policy_version"] or "unknown" if decision else "unknown"},
            "action": {"kind": action_kind if live else "simulation", "source": action["source"] if action else "unavailable", "version": ADAPTER_VERSION},
        }
        if basis:
            evidence["classification"] = {"kind": "simulation" if synthetic_classification else "model_derived", "source": basis["source"],
                                          "version": basis["model_version"], "confidence_is_probability": False}
        row = {"schema": FEATURE_SCHEMA, "key": f"{run_id}/{sequence}", "sequence": ordinal, "source_at": None,
               "decision": selected, "action": issued, "features": compact, "evidence": evidence,
               "source": {"bean_ref": {"run_id": run_id, "sequence": sequence}, "track_status": bean["status"],
                          "source_timestamp_ns": bean["updated_timestamp_ns"], "clock_domain": "registry source clock; not UTC",
                          "selection_decision": decision, "gate_cycle": action, "classification_decision_basis": basis,
                          "statistics_sample_count": raw_features.get("sample_count"), "calibration_comparability": "unknown"}}
        row["source"]["feature_mapping"] = feature_mapping
        row["source"]["classification_members"] = members
        row["source"]["statistics_coverage"] = {key: raw_features.get(key) for key in (
            "sample_count", "derived_observation_count", "sensor_edge_observation_count",
            "enrichment_fallback_observation_count", "minimum_measurement_view_count")}
        validate_particle(row, environment)
        rows.append(row)
        counts[f"decision_{selected}"] += 1
        counts[f"action_{issued}"] += 1
        counts[f"track_{bean['status']}"] += 1
    output.mkdir(parents=True)
    batches = []
    for offset in range(0, len(rows), 500):
        name = f"particles-{offset:08d}.json"
        path = output / name
        write_json(path, rows[offset:offset + 500])
        batches.append({"path": name, "sha256": sha256(path), "count": len(rows[offset:offset + 500])})
    manifest = {"schema": ADAPTER_VERSION, "environment": environment, "source_run_id": run_id,
                "registry_path": str(database), "session": session, "settings": settings,
                "registry_cursor": cursor, "statistics": statistics_ref, "particle_count": len(rows),
                "counts": dict(counts), "physical_coverage": "unknown", "batches": batches,
                "source_versions": {"adapter": ADAPTER_VERSION, "registry_schema": 3,
                                    "requested_recipe_match": "not established; recorded source settings are pinned separately",
                                    "settings_sha256": digest(settings), "policy_versions": sorted({d["policy_version"] or "unknown" for d in decisions.values()}),
                                    "classification_pool_versions": sorted({d["model_version"] or "unknown" for d in enrichments.values()}),
                                    "classification_model_versions": sorted({d["model_version"] or "unknown" for d in classifier_members.values()})}}
    manifest["fingerprint"] = digest(manifest)
    # Manifest is written last: an interrupted export is never a valid spool.
    write_json(output / "manifest.json", manifest)
    directory_fd = os.open(output, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return manifest


def import_spool(workflow, user_id, run_id, directory):
    with workflow.db.read() as s:
        require_role(get(s, User, user_id), "admin", "supervisor")
    directory = Path(directory).resolve()
    manifest = load_json(directory / "manifest.json")
    fingerprint = manifest.get("fingerprint")
    if manifest.get("schema") != ADAPTER_VERSION or digest({k: v for k, v in manifest.items() if k != "fingerprint"}) != fingerprint:
        raise DomainError("Invalid spool manifest fingerprint")
    batches, identities, sequences = [], set(), set()
    for entry in manifest["batches"]:
        path = (directory / entry["path"]).resolve()
        if path.parent != directory or sha256(path) != entry["sha256"]:
            raise DomainError("Spool batch path or hash invalid")
        rows = load_json(path)
        if len(rows) != entry["count"]:
            raise DomainError("Spool batch count mismatch")
        for row in rows:
            validate_particle(row, manifest["environment"])
            if row["key"] in identities or row["sequence"] in sequences:
                raise DomainError("Duplicate particle identity or sequence in spool")
            identities.add(row["key"])
            sequences.add(row["sequence"])
        batches.append((entry, rows))
    if sum(len(rows) for _, rows in batches) != manifest["particle_count"]:
        raise DomainError("Incomplete spool")
    if sequences != set(range(manifest["particle_count"])):
        raise DomainError("Spool source ordinal gap")
    with workflow.db.write() as s:
        user = get(s, User, user_id)
        require_role(user, "admin", "supervisor")
        run = get(s, Run, run_id)
        op = get(s, Operation, run.operation_id)
        if manifest["environment"] != workflow.db.environment or manifest["source_run_id"] != run.source_run_id:
            raise DomainError("Spool environment/source run does not match the workflow run")
        previous = s.scalar(select(SourceBundle).filter_by(run_id=run.id))
        if previous and previous.fingerprint != fingerprint:
            raise DomainError("A different source snapshot is already pinned; create a new operation boundary")
        if not previous:
            if op.status not in {"running", "paused", "failed", "finishing"}:
                raise DomainError("Cannot attach new evidence after reconciliation")
            s.add(SourceBundle(id=identity("source"), run_id=run.id, fingerprint=fingerprint,
                               manifest={**manifest, "spool_path": str(directory)}))
            audit(s, user, "source_bundle_attached", {"run_id": run.id, "fingerprint": fingerprint}, op)
    inserted = 0
    for entry, rows in batches:
        inserted += workflow.ingest(user_id, run_id, fingerprint + ":" + entry["path"], rows)
    return {"inserted": inserted, "source_particles": manifest["particle_count"], "fingerprint": fingerprint}
