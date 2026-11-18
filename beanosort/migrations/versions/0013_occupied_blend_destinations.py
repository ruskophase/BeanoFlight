"""Allow blends to use their pinned, fully reserved input container."""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = depends_on = None


def upgrade():
    # Existing sorting semantics stay intact. A blend must additionally include
    # the occupied destination's entire portion in its immutable input boundary.
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER destination_empty")
        op.execute("""CREATE TRIGGER destination_empty BEFORE INSERT ON destinations
            WHEN NEW.active AND EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.container_id AND active)
            AND NOT EXISTS (
                SELECT 1 FROM assignments a JOIN reservations r ON r.portion_id=a.portion_id
                JOIN operations o ON o.id=NEW.operation_id
                WHERE a.container_id=NEW.container_id AND a.active AND r.active
                AND r.operation_id=NEW.operation_id
                AND a.portion_id=json_extract(NEW.readiness,'$.existing_portion_id')
                AND r.mass_mg=json_extract(NEW.readiness,'$.existing_mass_mg')
                AND r.mass_mg=(SELECT COALESCE(SUM(delta_mg),0) FROM ledger WHERE portion_id=a.portion_id)
                AND (o.kind='sorting' OR (o.kind='blend'
                    AND a.id=json_extract(NEW.readiness,'$.existing_assignment_id')
                    AND EXISTS (SELECT 1 FROM operation_inputs i WHERE i.operation_id=NEW.operation_id
                        AND i.portion_id=a.portion_id AND i.mass_mg=r.mass_mg))))
            BEGIN SELECT RAISE(ABORT, 'Occupied destination requires pinned reserved contents'); END""")
    else:
        op.execute("""CREATE OR REPLACE FUNCTION guard_destination() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            PERFORM 1 FROM containers WHERE id=NEW.container_id FOR UPDATE;
            IF NEW.active AND EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.container_id AND active)
                AND NOT EXISTS (SELECT 1 FROM assignments a JOIN reservations r ON r.portion_id=a.portion_id
                    JOIN operations o ON o.id=NEW.operation_id
                    WHERE a.container_id=NEW.container_id AND a.active AND r.active AND r.operation_id=NEW.operation_id
                    AND a.portion_id=NEW.readiness->>'existing_portion_id'
                    AND r.mass_mg=(NEW.readiness->>'existing_mass_mg')::bigint
                    AND r.mass_mg=(SELECT COALESCE(SUM(delta_mg),0) FROM ledger WHERE portion_id=a.portion_id)
                    AND (o.kind='sorting' OR (o.kind='blend'
                        AND a.id=NEW.readiness->>'existing_assignment_id'
                        AND EXISTS (SELECT 1 FROM operation_inputs i WHERE i.operation_id=NEW.operation_id
                            AND i.portion_id=a.portion_id AND i.mass_mg=r.mass_mg))))
            THEN RAISE EXCEPTION 'Occupied destination requires pinned reserved contents'; END IF;
            RETURN NEW;
        END $$""")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
