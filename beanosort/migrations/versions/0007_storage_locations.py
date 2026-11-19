"""Register existing storage places and link every container to one location."""
from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = depends_on = None


def upgrade():
    op.create_table("locations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("name_key", sa.String(600), nullable=False, unique=True),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200"))
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if sqlite:
        # SQLite supports a nullable FK via ADD COLUMN; no table copy or lost
        # accounting/assignment triggers are needed. Guards require it after fill.
        op.execute("ALTER TABLE containers ADD COLUMN location_id VARCHAR(64) REFERENCES locations(id)")
    else:
        op.add_column("containers", sa.Column("location_id", sa.String(64), sa.ForeignKey("locations.id"), nullable=True))
    names = list(bind.execute(sa.text("SELECT DISTINCT location FROM containers ORDER BY location")).scalars())
    lookup = {}
    for original in names:
        name = (original or "").strip() or "Unspecified"
        if len(name) > 200:
            raise RuntimeError("Existing storage location exceeds 200 characters; review before migration")
        key = name.casefold()
        if key not in lookup:
            lid = "location-" + str(uuid4())
            lookup[key] = (lid, name)
            bind.execute(sa.text("INSERT INTO locations (id,created_at,name,name_key) VALUES (:id,:at,:name,:key)"),
                         {"id": lid, "at": datetime.now(timezone.utc).isoformat(), "name": name, "key": key})
        lid, canonical = lookup[key]
        bind.execute(sa.text("UPDATE containers SET location_id=:id,location=:name WHERE location=:original"),
                     {"id": lid, "name": canonical, "original": original})
    op.create_index("containers_by_location", "containers", ["location_id"])
    if sqlite:
        for action in ("INSERT", "UPDATE"):
            op.execute(f"""CREATE TRIGGER containers_location_{action.lower()} BEFORE {action} ON containers
                WHEN NEW.location_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM locations WHERE id=NEW.location_id AND name=NEW.location)
                BEGIN SELECT RAISE(ABORT, 'Choose a registered storage location'); END""")
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER locations_no_{action.lower()} BEFORE {action} ON locations BEGIN SELECT RAISE(ABORT, 'append-only locations'); END")
    else:
        op.execute("""CREATE FUNCTION guard_container_location() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.location_id IS NULL OR NOT EXISTS (SELECT 1 FROM locations WHERE id=NEW.location_id AND name=NEW.location) THEN
                RAISE EXCEPTION 'Choose a registered storage location';
            END IF;
            RETURN NEW;
        END $$""")
        op.execute("CREATE TRIGGER containers_location BEFORE INSERT OR UPDATE ON containers FOR EACH ROW EXECUTE FUNCTION guard_container_location()")
        op.execute("CREATE TRIGGER locations_immutable BEFORE UPDATE OR DELETE ON locations FOR EACH ROW EXECUTE FUNCTION reject_history_change()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
