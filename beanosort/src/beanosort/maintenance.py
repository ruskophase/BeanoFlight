"""Offline, recoverable maintenance for the isolated SQLite training database.

This prepares a NEW database file; it never clears a running or real database.
The caller must stop the training server before archiving and activating the copy.
"""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
from uuid import uuid4

from .models import now
from .quantities import DomainError


# Child-first foreign-key order. Configuration, reference samples, users,
# locations and vessels deliberately remain outside this boundary.
MATERIAL_TABLES = (
    "machine_commands", "machine_events", "machine_jobs",
    "run_summaries",
    "collection_weighings", "sorting_segments", "sorting_collections",
    "packs", "product_lots", "corrections", "handling_plans", "source_bundles",
    "monitor_events", "ingest_batches", "particles", "profiles", "simulation_assumptions",
    "destinations", "reservations", "lineage", "operation_inputs", "operation_outputs",
    "ledger", "measurements", "reconciliations", "assignments", "portions", "receipts",
    "runs", "operations",
)
CONFIG_TABLES = (
    "alembic_version", "world", "users", "containers", "locations", "versions",
    "simulation_settings", "reference_datasets",
)


def schema(connection):
    return connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()


def rows(connection, table):
    return connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()


def stage_training_inventory_reset(source, staged, backup, *, operator="admin"):
    """Archive source and prepare a verified empty-inventory copy offline.

    Refuses real inventory and pre-existing destinations. Append-only DELETE
    guards are removed only inside a transaction on the unserved staging file,
    then restored verbatim. The complete source/backup remain untouched.
    """
    source, staged, backup = (Path(path).resolve() for path in (source, staged, backup))
    if len({source, staged, backup}) != 3 or not source.is_file() or staged.exists() or backup.exists():
        raise DomainError("Choose an existing source and two distinct new staging/backup paths")
    staged.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)) as original:
        if original.execute("SELECT environment FROM world WHERE id=1").fetchone() != ("simulation",):
            raise DomainError("Offline inventory reset is restricted to isolated simulation databases")
        user = original.execute("SELECT id,role,active FROM users WHERE username=?", (operator,)).fetchone()
        if not user or user[1] != "admin" or not user[2]:
            raise DomainError("An active administrator must own the training inventory reset")
        original_schema = schema(original)
        expected_tables = set(CONFIG_TABLES) | set(MATERIAL_TABLES) | {"stations", "audit", "commands"}
        actual_tables = {name for (name,) in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual_tables != expected_tables:
            raise DomainError("Review the maintenance boundary for this database schema before resetting")
        if original.execute("SELECT 1 FROM machine_jobs WHERE state NOT IN ('prepared','completed','failed','cancelled') LIMIT 1").fetchone():
            raise DomainError("End active machine jobs and synchronize their journals before preparing an inventory reset")
        with closing(sqlite3.connect(backup)) as archive:
            original.backup(archive)
            backup.chmod(0o600)
            if archive.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or archive.execute("PRAGMA foreign_key_check").fetchall():
                raise DomainError("Training archive failed integrity checks")
            with closing(sqlite3.connect(staged)) as target:
                archive.backup(target)
    staged.chmod(0o600)
    with closing(sqlite3.connect(staged, isolation_level=None)) as target:
        target.execute("PRAGMA foreign_keys=ON")
        preserved = {table: rows(target, table) for table in CONFIG_TABLES}
        columns = [c[1] for c in target.execute("PRAGMA table_info(stations)")]
        station_definition = [column for column in columns if column != "last_received_at"]
        station_query = 'SELECT '+','.join('"'+c+'"' for c in station_definition)+' FROM stations ORDER BY id'
        stations = target.execute(station_query).fetchall()
        counts = {table: len(rows(target, table)) for table in MATERIAL_TABLES}
        config_audits = target.execute("SELECT * FROM audit WHERE operation_id IS NULL ORDER BY rowid").fetchall()
        kept_ids = {key for table in CONFIG_TABLES if table not in {"world", "alembic_version"}
                    for (key,) in target.execute(f'SELECT id FROM "{table}"')}
        kept_ids.update(key for (key,) in target.execute("SELECT id FROM stations"))
        # Commands.result_id is located by schema below rather than relying on
        # its physical column position during deletion/verification.
        command_columns = [c[1] for c in target.execute("PRAGMA table_info(commands)")]
        result_index = command_columns.index("result_id")
        config_commands = [row for row in rows(target, "commands") if row[result_index] in kept_ids]
        guards = [(name, sql) for kind, name, table, sql in original_schema
                  if kind == "trigger" and table in set(MATERIAL_TABLES) | {"audit", "commands"} and name.endswith("_no_delete")]
        try:
            target.execute("BEGIN IMMEDIATE")
            for name, _ in guards:
                target.execute(f'DROP TRIGGER "{name}"')
            target.execute("DELETE FROM audit WHERE operation_id IS NOT NULL")
            target.execute("CREATE TEMP TABLE reset_configuration_ids (id TEXT PRIMARY KEY)")
            target.executemany("INSERT INTO reset_configuration_ids VALUES (?)", [(key,) for key in kept_ids])
            target.execute("DELETE FROM commands WHERE result_id NOT IN (SELECT id FROM reset_configuration_ids)")
            for table in MATERIAL_TABLES:
                target.execute(f'DELETE FROM "{table}"')
            target.execute("UPDATE stations SET last_received_at=NULL")
            for _, sql in guards:
                target.execute(sql)
            data = {"reason": "Operator requested a fresh start with no received beans; preserve configuration",
                    "environment": "simulation", "archive": str(backup), "removed_record_counts": counts,
                    "physical_empty_or_clean_confirmation": False, "reference_population_preserved": True}
            target.execute("INSERT INTO audit (id,created_at,operator_id,station_id,operation_id,action,data) VALUES (?,?,?,NULL,NULL,?,?)",
                           ("audit-"+str(uuid4()), now(), user[0], "training_inventory_reset", json.dumps(data)))
            if schema(target) != original_schema or target.execute("PRAGMA foreign_key_check").fetchall():
                raise DomainError("Reset changed schema protections or left dangling foreign keys")
            for table, expected in preserved.items():
                if rows(target, table) != expected:
                    raise DomainError(f"Reset changed preserved configuration: {table}")
            if target.execute(station_query).fetchall() != stations:
                raise DomainError("Reset changed station definitions")
            if rows(target, "audit")[:-1] != config_audits:
                raise DomainError("Reset changed configuration audit history")
            if rows(target, "commands") != config_commands:
                raise DomainError("Reset changed retained configuration commands")
            if any(target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in MATERIAL_TABLES):
                raise DomainError("Reset left material records")
            target.execute("COMMIT")
        except BaseException:
            target.execute("ROLLBACK")
            raise
        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise DomainError("Staged reset failed integrity checks")
    return {"backup": str(backup), "staged": str(staged), "removed_record_counts": counts,
            "containers_preserved": len(preserved["containers"]), "locations_preserved": len(preserved["locations"]),
            "configuration_versions_preserved": len(preserved["versions"])}
