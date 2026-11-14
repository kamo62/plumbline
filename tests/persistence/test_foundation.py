from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from plumbline.persistence.migrations import HEAD, UnsupportedSchemaError, migrate, status
from plumbline.persistence.models import Metric, RawMetric, Run, init_engine_and_tables
from plumbline.persistence.sinks import PostgresSink


def test_concurrent_bootstrap_and_idempotent_snapshots(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert list(pool.map(migrate, [url] * 5)) == [HEAD] * 5
    assert status(url) == HEAD
    engine = init_engine_and_tables(url)
    sink = PostgresSink(engine)
    run_id, other_id = uuid4(), uuid4()
    result = {"raw_ado_data": {"items": [1], "when": datetime.now(timezone.utc)},
              "calculated_metrics": [{"key": "count", "value_float": 0, "value_json": {"ok": True}}]}
    context = {"run_id": str(run_id), "commit_sha": "same", "executed_at": datetime.now(timezone.utc)}
    sink.persist(result, context)
    sink.persist(result, context)
    sink.persist(result, {**context, "run_id": str(other_id)})
    with Session(engine) as session:
        assert len(session.exec(select(Run)).all()) == 2
        assert len(session.exec(select(RawMetric)).all()) == 2
        metrics = session.exec(select(Metric)).all()
        assert len(metrics) == 2 and metrics[0].metric_value == 0
        assert metrics[0].metric_json == {"ok": True}
    with engine.begin() as connection:
        with pytest.raises(Exception):
            connection.execute(text("INSERT INTO raw_metrics (id,run_id,source_system,source_key,raw_data,collected_at) VALUES ('a','missing','a','b','{}','2026-01-01')"))


def test_legacy_schema_refused_without_modification(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = init_engine_and_tables(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE runs (legacy TEXT)"))
        connection.execute(text("INSERT INTO runs VALUES ('keep')"))
    with pytest.raises(UnsupportedSchemaError, match="legacy schema"):
        migrate(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT legacy FROM runs")).scalar() == "keep"
        assert "alembic_version" not in inspect(connection).get_table_names()


def test_postgresql_baseline_compiles_offline():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from io import StringIO
    from plumbline.persistence.baseline import upgrade

    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    upgrade(Operations(context))
    sql = output.getvalue()
    assert "CREATE TABLE runs" in sql
    assert "FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE" in sql
    assert "UNIQUE (run_id, scope, metric_key)" in sql
    assert "timescaledb" not in sql.lower()


def test_api_reads_sqlite_run(tmp_path):
    import asyncio
    from plumbline.api.main import get_run_status, list_runs
    from plumbline.config.settings import Settings

    url = f"sqlite:///{tmp_path / 'api.db'}"
    migrate(url)
    sink = PostgresSink(init_engine_and_tables(url))
    run_id = uuid4()
    sink.persist({}, {"run_id": str(run_id), "executed_at": datetime.now(timezone.utc)})
    settings = Settings(database_url=url)
    assert get_run_status(run_id, settings)["id"] == str(run_id)
    assert any(row["id"] == str(run_id) for row in list_runs(settings=settings))
