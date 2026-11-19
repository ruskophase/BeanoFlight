"""Editable locations and deletion of empty vessels without deleting genealogy."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = depends_on = None


def upgrade():
    op.add_column("locations", sa.Column("revision", sa.Integer(), sa.CheckConstraint("revision > 0"), nullable=False, server_default="1"))
    op.add_column("containers", sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        op.execute("DROP TRIGGER locations_no_update")
        op.execute("DROP TRIGGER locations_no_delete")
        for action in ("INSERT", "UPDATE"):
            op.execute(f"DROP TRIGGER containers_location_{action.lower()}")
            op.execute(f"""CREATE TRIGGER containers_location_{action.lower()} BEFORE {action} ON containers
                WHEN NOT NEW.deleted AND (NEW.location_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM locations WHERE id=NEW.location_id AND name=NEW.location))
                BEGIN SELECT RAISE(ABORT, 'Choose a registered storage location'); END""")
        op.execute("""CREATE TRIGGER deleted_container_empty BEFORE UPDATE ON containers
            WHEN NEW.deleted AND (NEW.location_id IS NOT NULL
                OR EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.id AND active)
                OR EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.id AND active))
            BEGIN SELECT RAISE(ABORT, 'Deleted containers must be empty and removed from storage'); END""")
        op.execute("""CREATE TRIGGER deleted_container_empty_insert BEFORE INSERT ON containers
            WHEN NEW.deleted AND NEW.location_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'Deleted containers must be removed from storage'); END""")
        for table in ("assignments", "destinations"):
            for action in ("INSERT", "UPDATE"):
                op.execute(f"DROP TRIGGER {table}_container_active_{action.lower()}")
                op.execute(f"""CREATE TRIGGER {table}_container_active_{action.lower()} BEFORE {action} ON {table}
                    WHEN NEW.active AND EXISTS (SELECT 1 FROM containers WHERE id=NEW.container_id AND (archived OR deleted))
                    BEGIN SELECT RAISE(ABORT, 'Container is archived or deleted'); END""")
        op.execute("""CREATE TRIGGER locations_rename AFTER UPDATE OF name ON locations WHEN NEW.name != OLD.name
            BEGIN UPDATE containers SET location=NEW.name, revision=revision+1 WHERE location_id=NEW.id AND NOT deleted; END""")
        op.execute("""CREATE TRIGGER locations_empty BEFORE DELETE ON locations
            WHEN EXISTS (SELECT 1 FROM containers WHERE location_id=OLD.id)
            BEGIN SELECT RAISE(ABORT, 'Location still contains containers'); END""")
    else:
        op.execute("DROP TRIGGER locations_immutable ON locations")
        op.execute("""CREATE OR REPLACE FUNCTION guard_container_location() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NOT NEW.deleted AND (NEW.location_id IS NULL OR NOT EXISTS (SELECT 1 FROM locations WHERE id=NEW.location_id AND name=NEW.location)) THEN
                RAISE EXCEPTION 'Choose a registered storage location';
            END IF;
            IF NEW.deleted AND (NEW.location_id IS NOT NULL
                OR EXISTS (SELECT 1 FROM assignments WHERE container_id=NEW.id AND active)
                OR EXISTS (SELECT 1 FROM destinations WHERE container_id=NEW.id AND active)) THEN
                RAISE EXCEPTION 'Deleted containers must be empty and removed from storage';
            END IF;
            RETURN NEW;
        END $$""")
        op.execute("""CREATE OR REPLACE FUNCTION require_active_container() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE disabled boolean;
            BEGIN
                IF NEW.active THEN
                    SELECT (archived OR deleted) INTO disabled FROM containers WHERE id=NEW.container_id FOR UPDATE;
                    IF disabled THEN RAISE EXCEPTION 'Container is archived or deleted'; END IF;
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE FUNCTION sync_location_name() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.name IS DISTINCT FROM OLD.name THEN
                UPDATE containers SET location=NEW.name, revision=revision+1 WHERE location_id=NEW.id AND NOT deleted;
            END IF;
            RETURN NEW;
        END $$""")
        op.execute("CREATE TRIGGER locations_rename AFTER UPDATE OF name ON locations FOR EACH ROW EXECUTE FUNCTION sync_location_name()")
        op.execute("""CREATE FUNCTION require_empty_location() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF EXISTS (SELECT 1 FROM containers WHERE location_id=OLD.id) THEN
                RAISE EXCEPTION 'Location still contains containers';
            END IF;
            RETURN OLD;
        END $$""")
        op.execute("CREATE TRIGGER locations_empty BEFORE DELETE ON locations FOR EACH ROW EXECUTE FUNCTION require_empty_location()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
