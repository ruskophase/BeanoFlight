"""Immutable reference populations for simulated sorting recipes."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = depends_on = None


def upgrade():
    op.create_table("reference_datasets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("directory", sa.String(2048), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("operator_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False))
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER reference_datasets_no_{action.lower()} BEFORE {action} ON reference_datasets BEGIN SELECT RAISE(ABORT, 'append-only history'); END")
    else:
        op.execute("CREATE TRIGGER reference_datasets_immutable BEFORE UPDATE OR DELETE ON reference_datasets FOR EACH ROW EXECUTE FUNCTION reject_history_change()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
