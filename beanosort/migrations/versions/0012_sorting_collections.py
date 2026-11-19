"""Append-only collection boundaries and weighing evidence; no legacy backfill."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = depends_on = None


def upgrade():
    op.create_table("sorting_collections",
        sa.Column("id",sa.String(64),primary_key=True), sa.Column("created_at",sa.String(40),nullable=False),
        sa.Column("operation_id",sa.String(64),sa.ForeignKey("operations.id"),nullable=False),
        sa.Column("destination_id",sa.String(64),sa.ForeignKey("destinations.id"),nullable=False,unique=True),
        sa.Column("container_id",sa.String(64),sa.ForeignKey("containers.id"),nullable=False),
        sa.Column("stream",sa.String(30),nullable=False),sa.Column("code",sa.String(30),nullable=False),
        sa.Column("ordinal",sa.Integer,nullable=False),
        sa.Column("portion_id",sa.String(64),sa.ForeignKey("portions.id")),
        sa.UniqueConstraint("operation_id","container_id"),sa.UniqueConstraint("operation_id","code"),
        sa.UniqueConstraint("operation_id","stream","ordinal"),sa.CheckConstraint("ordinal > 0"),
        sa.CheckConstraint("stream IN ('selected','pass_through')"))
    op.create_table("sorting_segments",
        sa.Column("id",sa.String(64),primary_key=True),sa.Column("created_at",sa.String(40),nullable=False),
        sa.Column("operation_id",sa.String(64),sa.ForeignKey("operations.id"),nullable=False),
        sa.Column("ordinal",sa.Integer,nullable=False),sa.Column("first_sequence",sa.Integer),
        sa.Column("source_at",sa.String(40)),sa.Column("recipe_id",sa.String(64),sa.ForeignKey("versions.id"),nullable=False),
        sa.Column("selected_id",sa.String(64),sa.ForeignKey("sorting_collections.id")),
        sa.Column("pass_through_id",sa.String(64),sa.ForeignKey("sorting_collections.id"),nullable=False),
        sa.Column("operator_id",sa.String(64),sa.ForeignKey("users.id"),nullable=False),sa.Column("evidence",sa.JSON,nullable=False),
        sa.UniqueConstraint("operation_id","ordinal"),sa.CheckConstraint("ordinal >= 0"),
        sa.CheckConstraint("first_sequence IS NULL OR first_sequence >= 0"))
    op.create_table("collection_weighings",
        sa.Column("id",sa.String(64),primary_key=True),sa.Column("created_at",sa.String(40),nullable=False),
        sa.Column("collection_id",sa.String(64),sa.ForeignKey("sorting_collections.id"),nullable=False),
        sa.Column("version",sa.Integer,nullable=False),sa.Column("gross_mg",sa.BigInteger,nullable=False),
        sa.Column("tare_mg",sa.BigInteger,nullable=False),sa.Column("container_revision",sa.Integer,nullable=False),
        sa.Column("operator_id",sa.String(64),sa.ForeignKey("users.id"),nullable=False),sa.Column("evidence",sa.JSON,nullable=False),
        sa.UniqueConstraint("collection_id","version"),sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("tare_mg > 0 AND gross_mg >= tare_mg"),sa.CheckConstraint("container_revision > 0"))
    collection_check = """NOT EXISTS (SELECT 1 FROM destinations d WHERE d.id=NEW.destination_id
        AND d.operation_id=NEW.operation_id AND d.container_id=NEW.container_id AND d.stream=NEW.code)
        OR NEW.code <> CASE WHEN NEW.ordinal=1 THEN NEW.stream ELSE NEW.stream||'_'||CAST(NEW.ordinal AS TEXT) END"""
    segment_check = """NOT EXISTS (SELECT 1 FROM operations o WHERE o.id=NEW.operation_id AND o.kind='sorting'
        AND o.recipe_id=NEW.recipe_id AND o.status='paused')
        OR NOT EXISTS (SELECT 1 FROM sorting_collections c WHERE c.id=NEW.pass_through_id AND c.operation_id=NEW.operation_id AND c.stream='pass_through')
        OR (NEW.selected_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sorting_collections c WHERE c.id=NEW.selected_id AND c.operation_id=NEW.operation_id AND c.stream='selected'))
        OR (NEW.ordinal>0 AND NOT EXISTS (SELECT 1 FROM sorting_segments p WHERE p.operation_id=NEW.operation_id AND p.ordinal=NEW.ordinal-1
            AND ((p.first_sequence IS NOT NULL AND (NEW.first_sequence IS NULL OR NEW.first_sequence>=p.first_sequence))
                 OR (p.first_sequence IS NULL AND NEW.first_sequence IS NULL))))"""
    fields = ("id","created_at","operation_id","destination_id","container_id","stream","code","ordinal")
    sqlite=op.get_bind().dialect.name == "sqlite"
    mode="json_extract(v.data,'$.operation_mode')" if sqlite else "v.data->>'operation_mode'"
    segment_check+=f" OR (NEW.selected_id IS NULL AND NOT EXISTS (SELECT 1 FROM versions v WHERE v.id=NEW.recipe_id AND {mode}='hopper_emptying'))"
    if sqlite:
        for table in ("sorting_segments","collection_weighings"):
            for action in ("UPDATE","DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append-only history'); END")
        for name,table,condition in (("collection_binding","sorting_collections",collection_check),("segment_binding","sorting_segments",segment_check)):
            op.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'invalid collection boundary'); END")
        changed=" OR ".join(f"NEW.{field} IS NOT OLD.{field}" for field in fields)
        op.execute(f"CREATE TRIGGER collection_pinned BEFORE UPDATE ON sorting_collections WHEN {changed} OR (OLD.portion_id IS NOT NULL AND NEW.portion_id IS NOT OLD.portion_id) BEGIN SELECT RAISE(ABORT,'pinned collection'); END")
        op.execute("CREATE TRIGGER collection_no_delete BEFORE DELETE ON sorting_collections BEGIN SELECT RAISE(ABORT,'collection history'); END")
    else:
        for table in ("sorting_segments","collection_weighings"):
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_history_change()")
        for name,table,condition in (("collection_binding","sorting_collections",collection_check),("segment_binding","sorting_segments",segment_check)):
            op.execute(f"CREATE FUNCTION guard_{name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF ({condition}) THEN RAISE EXCEPTION 'invalid collection boundary'; END IF; RETURN NEW; END $$")
            op.execute(f"CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION guard_{name}()")
        changed=" OR ".join(f"NEW.{field} IS DISTINCT FROM OLD.{field}" for field in fields)
        op.execute(f"CREATE FUNCTION guard_collection_pinned() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {changed} OR (OLD.portion_id IS NOT NULL AND NEW.portion_id IS DISTINCT FROM OLD.portion_id) THEN RAISE EXCEPTION 'pinned collection'; END IF; RETURN NEW; END $$")
        op.execute("CREATE TRIGGER collection_pinned BEFORE UPDATE ON sorting_collections FOR EACH ROW EXECUTE FUNCTION guard_collection_pinned()")
        op.execute("CREATE TRIGGER collection_no_delete BEFORE DELETE ON sorting_collections FOR EACH ROW EXECUTE FUNCTION reject_history_change()")


def downgrade():
    raise RuntimeError("Restore a reviewed backup rather than deleting collection history")
