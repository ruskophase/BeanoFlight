"""Relational material identities. JSON holds evidence, never stock balances."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(prefix):
    return f"{prefix}-{uuid4()}"


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class World(Base):
    __tablename__ = "world"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    environment: Mapped[str] = mapped_column(String(16))
    __table_args__ = (CheckConstraint("id = 1"), CheckConstraint("environment IN ('real','simulation')"))


class User(Record, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("role IN ('operator','supervisor','admin')"),)


class Station(Record, Base):
    __tablename__ = "stations"
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    capabilities: Mapped[dict] = mapped_column(JSON)
    last_received_at: Mapped[str | None] = mapped_column(String(40))


class Location(Record, Base):
    __tablename__ = "locations"
    name: Mapped[str] = mapped_column(String(200), unique=True)
    name_key: Mapped[str] = mapped_column(String(600), unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    __table_args__ = (CheckConstraint("length(name) BETWEEN 1 AND 200"), CheckConstraint("revision > 0"))


class Container(Record, Base):
    __tablename__ = "containers"
    label: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(40), default="bin")
    location: Mapped[str] = mapped_column(String(200))
    # A required FK is enforced by migration guards without rebuilding legacy
    # containers in SQLite. The display string must match the referenced name.
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    capacity_mg: Mapped[int | None] = mapped_column(BigInteger)
    # Nulls represent historical registrations only. Migration 0005 requires
    # positive capacity and tare for every new container at the database boundary.
    tare_mg: Mapped[int | None] = mapped_column(BigInteger, CheckConstraint(
        "tare_mg IS NULL OR tare_mg > 0", name="container_tare_positive"))
    __table_args__ = (CheckConstraint("capacity_mg IS NULL OR capacity_mg > 0"), CheckConstraint("revision > 0"))


class Version(Record, Base):
    __tablename__ = "versions"
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (UniqueConstraint("kind", "name", "version"), CheckConstraint("version > 0"),
                     CheckConstraint("kind IN ('recipe','capture_model','reconciliation_policy','product_specification','blend_plan','packaging_specification')"))


class ReferenceDataset(Record, Base):
    __tablename__ = "reference_datasets"
    name: Mapped[str] = mapped_column(String(120))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    directory: Mapped[str] = mapped_column(String(2048))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class SimulationSettings(Record, Base):
    """Append-only assumptions; a material mean is not a stock measurement."""
    __tablename__ = "simulation_settings"
    version: Mapped[int] = mapped_column(Integer, unique=True)
    material_label: Mapped[str] = mapped_column(String(120))
    mean_mass_ug: Mapped[int] = mapped_column(BigInteger)
    capture_model_id: Mapped[str] = mapped_column(ForeignKey("versions.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(2000))
    provenance: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("version > 0"), CheckConstraint("mean_mass_ug > 0 AND mean_mass_ug <= 1000000000"))


class SimulationAssumption(Record, Base):
    """Immutable mass/count basis accepted for a receipt or prepared job."""
    __tablename__ = "simulation_assumptions"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    settings_id: Mapped[str] = mapped_column(ForeignKey("simulation_settings.id"))
    data: Mapped[dict] = mapped_column(JSON)


class Operation(Record, Base):
    __tablename__ = "operations"
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(32))
    release_status: Mapped[str] = mapped_column(String(30), default="not_reconciled")
    execution_method: Mapped[str] = mapped_column(String(20))
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    recipe_id: Mapped[str | None] = mapped_column(ForeignKey("versions.id"))
    capture_model_id: Mapped[str | None] = mapped_column(ForeignKey("versions.id"))
    policy_id: Mapped[str | None] = mapped_column(ForeignKey("versions.id"))
    planned_mg: Mapped[int | None] = mapped_column(BigInteger)
    boundary_mg: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("boundary_mg > 0"), CheckConstraint("planned_mg IS NULL OR (planned_mg > 0 AND planned_mg <= boundary_mg)"),
        CheckConstraint("status IN ('prepared','running','paused','failed','finishing','awaiting_reconciliation','completed','cancelled')"),
        CheckConstraint("release_status IN ('not_reconciled','held','released')"),
        CheckConstraint("execution_method IN ('manual','automatic','simulated','replay')"))


class Receipt(Record, Base):
    __tablename__ = "receipts"
    source_lot_id: Mapped[str] = mapped_column(String(64), unique=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    supplier: Mapped[str | None] = mapped_column(String(200))
    supplier_lot: Mapped[str | None] = mapped_column(String(200))
    origin: Mapped[str | None] = mapped_column(String(200))
    bean_type: Mapped[str | None] = mapped_column(String(200))
    purchase_date: Mapped[str | None] = mapped_column(String(10))
    received_date: Mapped[str] = mapped_column(String(10))
    notes: Mapped[str] = mapped_column(String(4000), default="")
    declarations: Mapped[dict] = mapped_column(JSON, default=dict)


class Portion(Record, Base):
    __tablename__ = "portions"
    created_by: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    receipt_id: Mapped[str | None] = mapped_column(ForeignKey("receipts.id"))
    status: Mapped[str] = mapped_column(String(20))
    quality: Mapped[str] = mapped_column(String(20))
    hold_reason: Mapped[str | None] = mapped_column(String(2000))
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"))
    __table_args__ = (CheckConstraint("status IN ('stock','wip','consumed')"), CheckConstraint("quality IN ('held','released')"))


class Assignment(Record, Base):
    __tablename__ = "assignments"
    container_id: Mapped[str] = mapped_column(ForeignKey("containers.id"))
    portion_id: Mapped[str] = mapped_column(ForeignKey("portions.id"))
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ended_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        Index("one_active_contents", "container_id", unique=True, sqlite_where=text("active = true"), postgresql_where=text("active = true")),
        Index("one_active_placement", "portion_id", unique=True, sqlite_where=text("active = true"), postgresql_where=text("active = true")),
    )


class Input(Base):
    __tablename__ = "operation_inputs"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    portion_id: Mapped[str] = mapped_column(ForeignKey("portions.id"), primary_key=True)
    mass_mg: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("mass_mg > 0"),)


class Output(Base):
    __tablename__ = "operation_outputs"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    portion_id: Mapped[str] = mapped_column(ForeignKey("portions.id"), primary_key=True)
    stream: Mapped[str] = mapped_column(String(30))
    mass_mg: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("operation_id", "stream"), CheckConstraint("mass_mg > 0"))


class Lineage(Base):
    __tablename__ = "lineage"
    parent_id: Mapped[str] = mapped_column(ForeignKey("portions.id"), primary_key=True)
    child_id: Mapped[str] = mapped_column(ForeignKey("portions.id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    contribution: Mapped[str] = mapped_column(String(50), default="ancestry_known_proportions_unknown")
    __table_args__ = (CheckConstraint("parent_id != child_id"),)


class Reservation(Record, Base):
    __tablename__ = "reservations"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    portion_id: Mapped[str] = mapped_column(ForeignKey("portions.id"))
    mass_mg: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("mass_mg > 0"), Index("one_active_reservation", "portion_id", unique=True,
        sqlite_where=text("active = true"), postgresql_where=text("active = true")))


class Destination(Record, Base):
    __tablename__ = "destinations"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    container_id: Mapped[str] = mapped_column(ForeignKey("containers.id"))
    stream: Mapped[str] = mapped_column(String(30))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    readiness: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("operation_id", "stream"), Index("one_active_destination", "container_id", unique=True,
        sqlite_where=text("active = true"), postgresql_where=text("active = true")))


class Ledger(Record, Base):
    __tablename__ = "ledger"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    portion_id: Mapped[str] = mapped_column(ForeignKey("portions.id"))
    delta_mg: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(200))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    __table_args__ = (UniqueConstraint("operation_id", "portion_id"), CheckConstraint("delta_mg != 0"))


class Measurement(Record, Base):
    __tablename__ = "measurements"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    role: Mapped[str] = mapped_column(String(30))
    mass_mg: Mapped[int] = mapped_column(BigInteger)
    provenance: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("operation_id", "role"), CheckConstraint("mass_mg >= 0"))


class Reconciliation(Record, Base):
    __tablename__ = "reconciliations"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    discrepancy_mg: Mapped[int] = mapped_column(BigInteger)
    accounting: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(2000))
    policy_id: Mapped[str] = mapped_column(ForeignKey("versions.id"))


class Run(Record, Base):
    __tablename__ = "runs"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    source_run_id: Mapped[str] = mapped_column(String(200))
    seed: Mapped[str | None] = mapped_column(String(200))
    expected_particles: Mapped[int | None] = mapped_column(Integer)
    source_versions: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("station_id", "source_run_id"),)


class MachineJob(Base):
    __tablename__ = "machine_jobs"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    remote_id: Mapped[str] = mapped_column(String(64), unique=True)
    endpoint: Mapped[str] = mapped_column(String(300))
    request: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="prepared")
    cursor: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[dict] = mapped_column(JSON, default=dict)


class MachineEvent(Base):
    __tablename__ = "machine_events"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)


class MachineCommand(Record, Base):
    __tablename__ = "machine_commands"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    action: Mapped[str] = mapped_column(String(30))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class IngestBatch(Record, Base):
    __tablename__ = "ingest_batches"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    batch_key: Mapped[str] = mapped_column(String(200))
    digest: Mapped[str] = mapped_column(String(64))
    source_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("run_id", "batch_key"),)


class RunSummary(Base):
    """Rebuildable derived statistics; never an inventory balance or observation."""
    __tablename__ = "run_summaries"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    particle_count: Mapped[int] = mapped_column(BigInteger)
    last_sequence: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("revision > 0"), CheckConstraint("particle_count >= 0"),
                     CheckConstraint("last_sequence IS NULL OR last_sequence >= 0"))


class Particle(Record, Base):
    __tablename__ = "particles"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    source_key: Mapped[str] = mapped_column(String(200))
    source_sequence: Mapped[int] = mapped_column(Integer)
    source_at: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    simulated_destination: Mapped[str | None] = mapped_column(String(30))
    __table_args__ = (UniqueConstraint("run_id", "source_key"), UniqueConstraint("run_id", "source_sequence"), CheckConstraint("source_sequence >= 0"))


class MonitorEvent(Record, Base):
    __tablename__ = "monitor_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    particle_id: Mapped[str] = mapped_column(ForeignKey("particles.id"))
    kind: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("particle_id", "kind"),)


class Profile(Record, Base):
    __tablename__ = "profiles"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    portion_id: Mapped[str | None] = mapped_column(ForeignKey("portions.id"))
    stream: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("operation_id", "stream", "version"),)


class Audit(Record, Base):
    __tablename__ = "audit"
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"))
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    action: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSON)


class Command(Base):
    __tablename__ = "commands"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    result_id: Mapped[str] = mapped_column(String(64))


class SourceBundle(Record, Base):
    __tablename__ = "source_bundles"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)


class HandlingPlan(Record, Base):
    __tablename__ = "handling_plans"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    specification_id: Mapped[str | None] = mapped_column(ForeignKey("versions.id"))
    data: Mapped[dict] = mapped_column(JSON)


class Correction(Record, Base):
    __tablename__ = "corrections"
    original_operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer)
    accounting: Mapped[dict] = mapped_column(JSON)
    discrepancy_mg: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(2000))
    __table_args__ = (UniqueConstraint("original_operation_id", "version"), CheckConstraint("version > 0"))


class ProductLot(Record, Base):
    __tablename__ = "product_lots"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    portion_id: Mapped[str] = mapped_column(ForeignKey("portions.id"), unique=True)
    specification_id: Mapped[str] = mapped_column(ForeignKey("versions.id"))
    sku: Mapped[str] = mapped_column(String(120))
    nominal_mg: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("nominal_mg > 0"),)


class Pack(Base):
    __tablename__ = "packs"
    lot_id: Mapped[str] = mapped_column(ForeignKey("product_lots.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    actual_net_mg: Mapped[int] = mapped_column(BigInteger)
    evidence: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("ordinal > 0"), CheckConstraint("actual_net_mg > 0"))


class SortingCollection(Record, Base):
    """One container fill; unchanged bins can span several segments."""
    __tablename__ = "sorting_collections"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    destination_id: Mapped[str] = mapped_column(ForeignKey("destinations.id"), unique=True)
    container_id: Mapped[str] = mapped_column(ForeignKey("containers.id"))
    stream: Mapped[str] = mapped_column(String(30))
    code: Mapped[str] = mapped_column(String(30))
    ordinal: Mapped[int] = mapped_column(Integer)
    portion_id: Mapped[str | None] = mapped_column(ForeignKey("portions.id"))
    __table_args__ = (UniqueConstraint("operation_id", "container_id"), UniqueConstraint("operation_id", "code"),
        UniqueConstraint("operation_id", "stream", "ordinal"), CheckConstraint("ordinal > 0"),
        CheckConstraint("stream IN ('selected','pass_through')"))


class SortingSegment(Record, Base):
    """Append-only assignment boundary in one namespaced sorter run."""
    __tablename__ = "sorting_segments"
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    first_sequence: Mapped[int | None] = mapped_column(Integer)
    source_at: Mapped[str | None] = mapped_column(String(40))
    recipe_id: Mapped[str] = mapped_column(ForeignKey("versions.id"))
    selected_id: Mapped[str | None] = mapped_column(ForeignKey("sorting_collections.id"))
    pass_through_id: Mapped[str] = mapped_column(ForeignKey("sorting_collections.id"))
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    evidence: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("operation_id", "ordinal"), CheckConstraint("ordinal >= 0"),
        CheckConstraint("first_sequence IS NULL OR first_sequence >= 0"),)


class CollectionWeighing(Record, Base):
    __tablename__ = "collection_weighings"
    collection_id: Mapped[str] = mapped_column(ForeignKey("sorting_collections.id"))
    version: Mapped[int] = mapped_column(Integer)
    gross_mg: Mapped[int] = mapped_column(BigInteger)
    tare_mg: Mapped[int] = mapped_column(BigInteger)
    container_revision: Mapped[int] = mapped_column(Integer)
    operator_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    evidence: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("collection_id", "version"), CheckConstraint("version > 0"),
        CheckConstraint("tare_mg > 0 AND gross_mg >= tare_mg"), CheckConstraint("container_revision > 0"))
