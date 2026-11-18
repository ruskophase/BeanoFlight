"""Require tare and capacity for new registrations without inventing legacy data."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # Existing containers retain NULL for unknown tare, and their original capacity.
    # Adding the nullable column avoids copying containers or disturbing FK history.
    op.add_column("containers", sa.Column("tare_mg", sa.BigInteger(), sa.CheckConstraint(
        "tare_mg IS NULL OR tare_mg > 0", name="container_tare_positive"), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""CREATE TRIGGER container_weights_required BEFORE INSERT ON containers
            WHEN NEW.capacity_mg IS NULL OR NEW.capacity_mg <= 0 OR NEW.tare_mg IS NULL OR NEW.tare_mg <= 0
            BEGIN SELECT RAISE(ABORT, 'New containers require positive capacity and tare'); END""")
        op.execute("""CREATE TRIGGER container_weights_not_cleared BEFORE UPDATE ON containers
            WHEN (OLD.capacity_mg IS NOT NULL AND NEW.capacity_mg IS NULL)
              OR (OLD.tare_mg IS NOT NULL AND NEW.tare_mg IS NULL)
            BEGIN SELECT RAISE(ABORT, 'Registered container weights cannot be cleared'); END""")
    else:
        op.execute("""CREATE FUNCTION guard_container_weights() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.capacity_mg IS NULL OR NEW.capacity_mg <= 0 OR NEW.tare_mg IS NULL OR NEW.tare_mg <= 0 THEN
                        RAISE EXCEPTION 'New containers require positive capacity and tare';
                    END IF;
                ELSIF (OLD.capacity_mg IS NOT NULL AND NEW.capacity_mg IS NULL)
                   OR (OLD.tare_mg IS NOT NULL AND NEW.tare_mg IS NULL) THEN
                    RAISE EXCEPTION 'Registered container weights cannot be cleared';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("CREATE TRIGGER container_weights_required BEFORE INSERT OR UPDATE ON containers FOR EACH ROW EXECUTE FUNCTION guard_container_weights()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
