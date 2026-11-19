import os
from alembic import context
from beanosort.db import Database
from beanosort.models import Base

url = context.config.attributes.get("database_url") or os.environ["BEANOSORT_DATABASE_URL"]
database = Database(url, "simulation")
with database.engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
