"""LAN machine execution, retryable commands and numerical journal cursors."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = depends_on = None

def upgrade():
    op.create_table("machine_jobs",
        sa.Column("operation_id",sa.String(64),sa.ForeignKey("operations.id"),primary_key=True),
        sa.Column("remote_id",sa.String(64),unique=True,nullable=False),
        sa.Column("endpoint",sa.String(300),nullable=False),sa.Column("request",sa.JSON(),nullable=False),
        sa.Column("state",sa.String(30),nullable=False),sa.Column("cursor",sa.BigInteger(),nullable=False),
        sa.Column("status",sa.JSON(),nullable=False))
    op.create_table("machine_events",
        sa.Column("operation_id",sa.String(64),sa.ForeignKey("operations.id"),primary_key=True),
        sa.Column("sequence",sa.BigInteger(),primary_key=True),sa.Column("digest",sa.String(64),nullable=False),
        sa.Column("data",sa.JSON(),nullable=False))
    op.create_table("machine_commands",
        sa.Column("id",sa.String(64),primary_key=True),sa.Column("created_at",sa.String(40),nullable=False),
        sa.Column("operation_id",sa.String(64),sa.ForeignKey("operations.id"),nullable=False),
        sa.Column("action",sa.String(30),nullable=False),sa.Column("acknowledged",sa.Boolean(),nullable=False))
    op.create_index("ix_machine_commands_pending","machine_commands",["operation_id","acknowledged"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER machine_events_no_{action.lower()} BEFORE {action} ON machine_events BEGIN SELECT RAISE(ABORT, 'append-only history'); END")
        op.execute("CREATE TRIGGER machine_jobs_pinned BEFORE UPDATE ON machine_jobs WHEN NEW.remote_id IS NOT OLD.remote_id OR NEW.endpoint IS NOT OLD.endpoint OR NEW.request IS NOT OLD.request BEGIN SELECT RAISE(ABORT, 'pinned machine manifest'); END")
    else:
        op.execute("CREATE TRIGGER machine_events_immutable BEFORE UPDATE OR DELETE ON machine_events FOR EACH ROW EXECUTE FUNCTION reject_history_change()")
        op.execute("""CREATE FUNCTION guard_machine_manifest() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.remote_id IS DISTINCT FROM OLD.remote_id OR NEW.endpoint IS DISTINCT FROM OLD.endpoint OR NEW.request::text IS DISTINCT FROM OLD.request::text
            THEN RAISE EXCEPTION 'pinned machine manifest'; END IF; RETURN NEW; END $$""")
        op.execute("CREATE TRIGGER machine_jobs_pinned BEFORE UPDATE ON machine_jobs FOR EACH ROW EXECUTE FUNCTION guard_machine_manifest()")

def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup")
