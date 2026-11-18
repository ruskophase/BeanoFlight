"""Rebuildable incremental summaries; original observations and accounting unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = depends_on = None


def upgrade():
    op.create_table("run_summaries",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("scope_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("particle_count", sa.BigInteger(), nullable=False),
        sa.Column("last_sequence", sa.Integer()),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.CheckConstraint("revision > 0"), sa.CheckConstraint("particle_count >= 0"),
        sa.CheckConstraint("last_sequence IS NULL OR last_sequence >= 0"))


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
