"""Append-only history and database-level accounting guards."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None

# Freeze the migration's schema contract rather than importing mutable models.
IMMUTABLE = ("versions", "receipts", "ledger", "measurements", "reconciliations", "operation_inputs", "operation_outputs",
             "lineage", "runs", "ingest_batches", "particles", "monitor_events", "profiles", "audit", "commands", "world")
PINNED = ("kind", "execution_method", "station_id", "operator_id", "recipe_id", "capture_model_id", "policy_id", "planned_mg", "boundary_mg")


def upgrade():
    op.add_column("portions", sa.Column("hold_reason", sa.String(2000), nullable=True))
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        for table in IMMUTABLE:
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only history'); END")
        changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in PINNED)
        op.execute(f"CREATE TRIGGER operations_pinned BEFORE UPDATE ON operations WHEN {changed} BEGIN SELECT RAISE(ABORT, 'pinned operation configuration'); END")
        op.execute("""CREATE TRIGGER ledger_nonnegative BEFORE INSERT ON ledger
            WHEN NEW.delta_mg + COALESCE((SELECT SUM(delta_mg) FROM ledger WHERE portion_id = NEW.portion_id),0) < 0
            BEGIN SELECT RAISE(ABORT, 'negative stock'); END""")
        op.execute("""CREATE TRIGGER lineage_acyclic BEFORE INSERT ON lineage WHEN EXISTS (
            WITH RECURSIVE descendants(id) AS (SELECT NEW.child_id UNION SELECT child_id FROM lineage JOIN descendants ON parent_id = descendants.id)
            SELECT 1 FROM descendants WHERE id = NEW.parent_id)
            BEGIN SELECT RAISE(ABORT, 'material ancestry cycle'); END""")
        op.execute("""CREATE TRIGGER reservation_valid BEFORE INSERT ON reservations WHEN NEW.active AND (
            NEW.mass_mg > COALESCE((SELECT SUM(delta_mg) FROM ledger WHERE portion_id=NEW.portion_id),0)
            OR NOT EXISTS (SELECT 1 FROM portions WHERE id=NEW.portion_id AND status='stock' AND quality='released'))
            BEGIN SELECT RAISE(ABORT, 'unavailable material'); END""")
        op.execute("""CREATE TRIGGER destination_empty BEFORE INSERT ON destinations WHEN NEW.active AND
            EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.container_id AND active)
            BEGIN SELECT RAISE(ABORT, 'occupied destination'); END""")
        op.execute("""CREATE TRIGGER assignment_unclaimed BEFORE INSERT ON assignments WHEN NEW.active AND
            EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.container_id AND active AND operation_id != NEW.operation_id)
            BEGIN SELECT RAISE(ABORT, 'reserved destination'); END""")
    else:
        op.execute("""CREATE FUNCTION reject_history_change() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'append-only history'; END $$""")
        for table in IMMUTABLE:
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_history_change()")
        changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in PINNED)
        op.execute(f"""CREATE FUNCTION guard_operation_config() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {changed} THEN RAISE EXCEPTION 'pinned operation configuration'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER operation_config BEFORE UPDATE ON operations FOR EACH ROW EXECUTE FUNCTION guard_operation_config()")
        op.execute("""CREATE FUNCTION guard_ledger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM portions WHERE id=NEW.portion_id FOR UPDATE;
            IF NEW.delta_mg + COALESCE((SELECT SUM(delta_mg) FROM ledger WHERE portion_id=NEW.portion_id),0) < 0
            THEN RAISE EXCEPTION 'negative stock'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER ledger_nonnegative BEFORE INSERT ON ledger FOR EACH ROW EXECUTE FUNCTION guard_ledger()")
        op.execute("""CREATE FUNCTION guard_lineage() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM world WHERE id=1 FOR UPDATE;
            IF EXISTS (WITH RECURSIVE descendants(id) AS (
                SELECT NEW.child_id UNION SELECT child_id FROM lineage JOIN descendants ON parent_id=descendants.id)
                SELECT 1 FROM descendants WHERE id=NEW.parent_id)
            THEN RAISE EXCEPTION 'material ancestry cycle'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER lineage_acyclic BEFORE INSERT ON lineage FOR EACH ROW EXECUTE FUNCTION guard_lineage()")
        op.execute("""CREATE FUNCTION guard_reservation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM portions WHERE id=NEW.portion_id FOR UPDATE;
            IF NEW.active AND (NEW.mass_mg > COALESCE((SELECT SUM(delta_mg) FROM ledger WHERE portion_id=NEW.portion_id),0)
                OR NOT EXISTS (SELECT 1 FROM portions WHERE id=NEW.portion_id AND status='stock' AND quality='released'))
            THEN RAISE EXCEPTION 'unavailable material'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER reservation_valid BEFORE INSERT ON reservations FOR EACH ROW EXECUTE FUNCTION guard_reservation()")
        op.execute("""CREATE FUNCTION guard_destination() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM containers WHERE id=NEW.container_id FOR UPDATE;
            IF NEW.active AND EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.container_id AND active)
            THEN RAISE EXCEPTION 'occupied destination'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER destination_empty BEFORE INSERT ON destinations FOR EACH ROW EXECUTE FUNCTION guard_destination()")
        op.execute("""CREATE FUNCTION guard_assignment() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM containers WHERE id=NEW.container_id FOR UPDATE;
            IF NEW.active AND EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.container_id AND active AND operation_id != NEW.operation_id)
            THEN RAISE EXCEPTION 'reserved destination'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER assignment_unclaimed BEFORE INSERT ON assignments FOR EACH ROW EXECUTE FUNCTION guard_assignment()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
