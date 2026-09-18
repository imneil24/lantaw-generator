from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


def get_engine(dsn: str):
    return create_engine(dsn, pool_pre_ping=True)


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def apply_column_additions(engine) -> None:
    """Adds columns introduced after initial table creation.

    Base.metadata.create_all() (called alongside this) only creates
    missing tables — it never alters existing ones, so a new column added
    to a model (e.g. Job.updated_at) needs an explicit ALTER TABLE against
    a database that already has the table. There is no migration
    framework in this repo. Checking existing columns via inspect() first
    (rather than "ADD COLUMN IF NOT EXISTS") keeps this portable across
    Postgres (production) and SQLite (tests/local dev) — SQLite's ALTER
    TABLE doesn't support the IF NOT EXISTS clause.
    """
    existing_columns = {col["name"] for col in inspect(engine).get_columns("jobs")}
    if "updated_at" not in existing_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN updated_at TIMESTAMP"))
