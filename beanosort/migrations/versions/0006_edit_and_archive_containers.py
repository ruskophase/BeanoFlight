"""Editable container revisions and archived vessels, preserving existing identity."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = depends_on = None


def upgrade():
    op.add_column("containers", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("containers", sa.Column("revision", sa.Integer(), sa.CheckConstraint("revision > 0"), nullable=False, server_default="1"))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""CREATE TRIGGER archived_container_empty BEFORE UPDATE ON containers
            WHEN NEW.archived AND (EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.id AND active)
              OR EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.id AND active))
            BEGIN SELECT RAISE(ABORT, 'Container has contents or an active job'); END""")
        op.execute("""CREATE TRIGGER container_capacity_contents BEFORE UPDATE ON containers
            WHEN NEW.capacity_mg IS NOT NULL AND NEW.capacity_mg < COALESCE((
              SELECT SUM(l.delta_mg) FROM ledger l JOIN assignments a ON a.portion_id=l.portion_id
              WHERE a.container_id=NEW.id AND a.active),0)
            BEGIN SELECT RAISE(ABORT, 'Capacity is below current material mass'); END""")
        for table in ("assignments", "destinations"):
            for action in ("INSERT", "UPDATE"):
                op.execute(f"""CREATE TRIGGER {table}_container_active_{action.lower()} BEFORE {action} ON {table}
                    WHEN NEW.active AND EXISTS (SELECT 1 FROM containers WHERE id=NEW.container_id AND archived)
                    BEGIN SELECT RAISE(ABORT, 'Container is archived'); END""")
    else:
        op.execute("""CREATE FUNCTION guard_container_use() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.archived AND (EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.id AND active)
                OR EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.id AND active)) THEN
                RAISE EXCEPTION 'Container has contents or an active job';
            END IF;
            IF NEW.capacity_mg IS NOT NULL AND NEW.capacity_mg < COALESCE((SELECT SUM(l.delta_mg)
                FROM ledger l JOIN assignments a ON a.portion_id=l.portion_id WHERE a.container_id=NEW.id AND a.active),0) THEN
                RAISE EXCEPTION 'Capacity is below current material mass';
            END IF;
            RETURN NEW;
        END $$""")
        op.execute("CREATE TRIGGER container_use BEFORE UPDATE ON containers FOR EACH ROW EXECUTE FUNCTION guard_container_use()")
        op.execute("""CREATE FUNCTION require_active_container() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE disabled boolean;
            BEGIN
                IF NEW.active THEN
                    SELECT archived INTO disabled FROM containers WHERE id=NEW.container_id FOR UPDATE;
                    IF disabled THEN RAISE EXCEPTION 'Container is archived'; END IF;
                END IF;
                RETURN NEW;
            END $$""")
        for table in ("assignments", "destinations"):
            op.execute(f"CREATE TRIGGER {table}_container_active BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION require_active_container()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
