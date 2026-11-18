from contextlib import contextmanager

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from .models import World


class Database:
    def __init__(self, url, environment):
        self.environment = environment
        self.engine = create_engine(url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def configure(connection, _):
                connection.isolation_level = None
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=15000")

            @event.listens_for(self.engine, "begin")
            def begin(connection):
                options = connection.get_execution_options()
                connection.exec_driver_sql("PRAGMA query_only=" + ("ON" if options.get("beanosort_read_only") else "OFF"))
                connection.exec_driver_sql("BEGIN IMMEDIATE" if options.get("beanosort_write") else "BEGIN")

    def enable_wal(self):
        """Provision SQLite concurrency outside transactions, before serving requests."""
        if self.engine.dialect.name != "sqlite":
            return
        connection = self.engine.raw_connection()
        try:
            connection.execute("PRAGMA query_only=OFF")
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode not in {"wal", "memory"}:
                raise RuntimeError("Could not enable SQLite WAL")
        finally:
            connection.close()

    def check_world(self, session):
        world = session.get(World, 1)
        if world is None or world.environment != self.environment:
            raise RuntimeError("Database environment mismatch; initialize a separate database for real/simulation")

    @contextmanager
    def read(self):
        with Session(self.engine, expire_on_commit=False) as session:
            if self.engine.dialect.name == "sqlite":
                session.connection(execution_options={"beanosort_read_only": True})
            self.check_world(session)
            yield session

    @contextmanager
    def write(self):
        # One short server-wide accounting lock in Phase 1. No lock is held
        # during processing or simulation generation. PostgreSQL locks the row;
        # SQLite's explicit BEGIN IMMEDIATE serializes writers across processes.
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            if self.engine.dialect.name == "sqlite":
                session.connection(execution_options={"beanosort_write": True})
            session.execute(select(World).where(World.id == 1).with_for_update()).scalar_one()
            self.check_world(session)
            yield session


APPEND_ONLY = ("versions", "receipts", "ledger", "measurements", "reconciliations", "operation_inputs", "operation_outputs", "lineage", "runs", "ingest_batches", "particles", "monitor_events", "profiles", "audit", "commands", "world", "source_bundles", "handling_plans", "corrections", "product_lots", "packs", "simulation_settings", "simulation_assumptions", "reference_datasets")

APPEND_ONLY += ("sorting_segments", "collection_weighings", "machine_events")
