from sqlalchemy import create_engine, inspect, text
from app.db import apply_column_additions


def test_apply_column_additions_adds_missing_updated_at_column():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE jobs (
            id VARCHAR(36) PRIMARY KEY, type VARCHAR(16), prompt VARCHAR(2000),
            duration FLOAT, status VARCHAR(16), retry_count INTEGER,
            result_key VARCHAR(512), runpod_job_id VARCHAR(64), created_at DATETIME
        )"""))

    apply_column_additions(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("jobs")}
    assert "updated_at" in columns


def test_apply_column_additions_is_idempotent_when_column_already_exists():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE jobs (
            id VARCHAR(36) PRIMARY KEY, type VARCHAR(16), prompt VARCHAR(2000),
            duration FLOAT, status VARCHAR(16), retry_count INTEGER,
            result_key VARCHAR(512), runpod_job_id VARCHAR(64), created_at DATETIME,
            updated_at DATETIME
        )"""))

    apply_column_additions(engine)  # must not raise on an already-migrated table
