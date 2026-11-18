"""Register the manual Packaging desk without changing existing stock."""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = depends_on = None


def upgrade():
    stations = sa.table("stations", sa.column("id", sa.String), sa.column("created_at", sa.String),
                        sa.column("name", sa.String), sa.column("kind", sa.String), sa.column("capabilities", sa.JSON))
    connection = op.get_bind()
    existing = connection.execute(sa.select(stations.c.kind).where(stations.c.name == "Packaging desk")).scalar_one_or_none()
    if existing is not None:
        if existing != "packaging":
            raise RuntimeError("An incompatible station named Packaging desk already exists; review it before upgrading")
        return
    connection.execute(stations.insert().values(id="station-packaging-desk", created_at=datetime.now(timezone.utc).isoformat(),
        name="Packaging desk", kind="packaging", capabilities={"execution_methods": ["manual", "simulated"], "hardware_control": False}))


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")
