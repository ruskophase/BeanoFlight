"""Versioned simulation assumptions and mass-based particle estimates."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("simulation_settings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("material_label", sa.String(120), nullable=False),
        sa.Column("mean_mass_ug", sa.BigInteger(), nullable=False),
        sa.Column("capture_model_id", sa.String(64), sa.ForeignKey("versions.id"), nullable=False),
        sa.Column("operator_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("mean_mass_ug > 0 AND mean_mass_ug <= 1000000000"))
    op.create_table("simulation_assumptions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("operation_id", sa.String(64), sa.ForeignKey("operations.id"), nullable=False, unique=True),
        sa.Column("settings_id", sa.String(64), sa.ForeignKey("simulation_settings.id"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False))
    for table in ("simulation_settings", "simulation_assumptions"):
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only history'); END")
        else:
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_history_change()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
