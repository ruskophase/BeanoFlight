"""Allow existing overfilled vessels to move without relaxing capacity reductions."""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = depends_on = None


def upgrade():
    # Receiving may post up to 120% of the nominal capacity. Subsequent name,
    # location, tare and revision updates must not reject that existing load.
    # Continue rejecting capacity reductions below the current material mass.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER container_capacity_contents")
        op.execute("""CREATE TRIGGER container_capacity_contents BEFORE UPDATE ON containers
            WHEN NEW.capacity_mg IS NOT NULL
              AND (OLD.capacity_mg IS NULL OR NEW.capacity_mg < OLD.capacity_mg)
              AND NEW.capacity_mg < COALESCE((
                SELECT SUM(l.delta_mg) FROM ledger l JOIN assignments a ON a.portion_id=l.portion_id
                WHERE a.container_id=NEW.id AND a.active),0)
            BEGIN SELECT RAISE(ABORT, 'Capacity cannot be reduced below current material mass'); END""")
    else:
        op.execute("""CREATE OR REPLACE FUNCTION guard_container_use() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.archived AND (EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.id AND active)
                OR EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.id AND active)) THEN
                RAISE EXCEPTION 'Container has contents or an active job';
            END IF;
            IF NEW.capacity_mg IS NOT NULL
                AND (OLD.capacity_mg IS NULL OR NEW.capacity_mg < OLD.capacity_mg)
                AND NEW.capacity_mg < COALESCE((SELECT SUM(l.delta_mg)
                    FROM ledger l JOIN assignments a ON a.portion_id=l.portion_id
                    WHERE a.container_id=NEW.id AND a.active),0) THEN
                RAISE EXCEPTION 'Capacity cannot be reduced below current material mass';
            END IF;
            RETURN NEW;
        END $$""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
