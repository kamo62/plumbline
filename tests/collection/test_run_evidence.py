import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import Session

from plumbline.api.models import CollectionConfig
from plumbline.collection.projection import project_snapshot
from plumbline.collection.store import (
    checkpoints, get_run_snapshot_id, load_snapshot, run_evidence, snapshots,
)
from plumbline.config.settings import Settings
from plumbline.connections.models import Connection
from plumbline.jobs import queue
from plumbline.jobs.models import Job
from plumbline.jobs.worker import run_once
from plumbline.persistence.migrations import HEAD, REVISIONS, migrate, status
from plumbline.persistence.models import Metric, RawMetric, Run, init_engine_and_tables
from plumbline.persistence.revision_0007_run_evidence import upgrade
from plumbline.persistence.sinks import PostgresSink
from plumbline.providers.synthetic import SyntheticProvider, WINDOW
from plumbline.providers.contract import Window
from plumbline.runner import execute_collection_run


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'evidence.db'}"
    migrate(url)
    return url, init_engine_and_tables(url)


def synthetic():
    return asyncio.run(SyntheticProvider("syn").collect(["Alpha", "Beta"], WINDOW))


def payload(snapshot):
    return {"provider_snapshot": snapshot, "raw_ado_data": {"evidence": "original"},
            "calculated_metrics": [{"key": "count", "value_float": 1}]}


def count(conn, table):
    return conn.scalar(sa.select(sa.func.count()).select_from(table))


def without_generated_times(value):
    if isinstance(value, dict):
        return {key: without_generated_times(item) for key, item in value.items() if key != "last_update_time"}
    if isinstance(value, list):
        return [without_generated_times(item) for item in value]
    return value


def test_synthetic_runner_and_worker_persist_the_same_original_snapshot(database):
    url, engine = database
    settings = Settings(database_url=url, azure_devops_org="example", azure_devops_pat="dummy-secret",
                        exemptions_file=None)
    config = CollectionConfig(connection_id="syn", all_projects=True, write_db=True, db_strict=True,
                              write_files=False, include_maturity=False)
    original = synthetic()
    supplied_id = str(uuid4()).upper()
    standalone = asyncio.run(execute_collection_run(config, settings, supplied_id, connection_provider="synthetic"))
    assert standalone["status"] == "succeeded", standalone
    assert standalone["id"] == supplied_id.lower()
    assert standalone["outputs"] == {}
    with Session(engine) as session:
        session.add(Connection(id="syn", provider="synthetic", name="Demo", config={}, capabilities=[]))
        session.commit()
    worker_id = queue.enqueue(engine, config, settings)
    assert asyncio.run(run_once(engine, settings))
    with Session(engine) as session:
        job = session.execute(sa.select(Job).where(Job.run_id == worker_id)).scalar_one()
        assert job.status == "succeeded", job.error
        assert job.result["snapshot_id"] == standalone["snapshot_id"]
        assert job.result["outputs"] == {}
        assert job.result["completeness"]["complete"] is False
        expected_raw = PostgresSink(engine)._sanitize_raw_data(project_snapshot(original))
        for run_id in (standalone["id"], worker_id):
            raw = session.execute(sa.select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one()
            assert without_generated_times(raw.raw_data) == without_generated_times(expected_raw)
    with engine.connect() as conn:
        for run_id in (standalone["id"], worker_id):
            assert get_run_snapshot_id(conn, run_id.upper()) == standalone["snapshot_id"]
        assert get_run_snapshot_id(conn, uuid4()) is None
        restored = load_snapshot(conn, standalone["snapshot_id"])
        assert restored == original
        assert [b.fields["tests"].state.value for b in restored.builds] == ["partial", "failed"]
        assert restored.builds[0].fields["tests"].value["results"][0]["passed"] == 8
        assert count(conn, snapshots) == 1 and count(conn, run_evidence) == 2


def test_mocked_ado_runner_and_worker_store_original_redacted_snapshots(database, monkeypatch):
    from plumbline.backend.api.ado_client import AzureDevOpsClient

    url, engine = database
    secret = "dummy-ado-secret"
    settings = Settings(database_url=url, azure_devops_org="example", azure_devops_pat=secret,
                        exemptions_file=None)
    seen = []

    async def request(client, url, session, params=None, **kwargs):
        if url.endswith("/_apis/projects/Alpha"):
            return {"id": "p1", "name": "Alpha", "description": "echo " + secret}, {}
        if url.endswith("/_apis/git/repositories"):
            return {"value": [{"id": "r1", "name": "repo", "defaultBranch": "refs/heads/main"}]}, {}
        return {"value": []}, {}

    original_persist = PostgresSink.persist

    def capture(sink, results, context):
        seen.append((copy.deepcopy(results["provider_snapshot"]), copy.deepcopy(results["raw_ado_data"])))
        return original_persist(sink, results, context)

    monkeypatch.setattr(AzureDevOpsClient, "_request_json", request)
    monkeypatch.setattr(PostgresSink, "persist", capture)
    config = CollectionConfig(project="Alpha", write_db=True, db_strict=True, write_files=False,
                              include_maturity=False)
    standalone = asyncio.run(execute_collection_run(config, settings))
    assert standalone["status"] == "succeeded", standalone
    worker_id = queue.enqueue(engine, config, settings)
    assert asyncio.run(run_once(engine, settings))
    with Session(engine) as session:
        job = session.execute(sa.select(Job).where(Job.run_id == worker_id)).scalar_one()
        assert job.status == "succeeded", job.error
        ids = [(standalone["id"], standalone["snapshot_id"]), (worker_id, job.result["snapshot_id"])]
        for (run_id, _), (snapshot, raw) in zip(ids, seen, strict=True):
            stored = session.execute(sa.select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one()
            assert stored.raw_data == PostgresSink(engine)._sanitize_raw_data(raw)
            assert secret not in json.dumps(stored.raw_data) + repr(snapshot)
    with engine.connect() as conn:
        for (run_id, snapshot_id), (original, _) in zip(ids, seen, strict=True):
            assert get_run_snapshot_id(conn, run_id) == snapshot_id
            assert load_snapshot(conn, snapshot_id) == original
            assert original.projects[0].fields["project"].value["description"] == "echo [REDACTED]"
            assert original.projects[0].fields["project"].state.value == "observed"


def test_synthetic_database_snapshot_redaction_matches_worker(database):
    url, engine = database
    settings = Settings(database_url=url, azure_devops_org="example", azure_devops_pat="one",
                        exemptions_file=None)
    config = CollectionConfig(connection_id="syn", all_projects=True, write_db=True, db_strict=True,
                              write_files=False, include_maturity=False)
    with Session(engine) as session:
        session.add(Connection(id="syn", provider="synthetic", name="Demo", config={}, capabilities=[]))
        session.commit()
    standalone = asyncio.run(execute_collection_run(config, settings, connection_provider="synthetic"))
    assert standalone["status"] == "succeeded", standalone
    worker_id = queue.enqueue(engine, config, settings)
    assert asyncio.run(run_once(engine, settings))
    with Session(engine) as session:
        job = session.execute(sa.select(Job).where(Job.run_id == worker_id)).scalar_one()
        assert job.status == "succeeded", job.error
        assert job.result["snapshot_id"] == standalone["snapshot_id"]
        raw = [session.execute(sa.select(RawMetric.raw_data).where(RawMetric.run_id == run_id)).scalar_one()
               for run_id in (standalone["id"], worker_id)]
        assert without_generated_times(raw[0]) == without_generated_times(raw[1])
        assert all('one' not in json.dumps(item) for item in raw)
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, standalone["snapshot_id"])
        assert snapshot.repositories[0].fields["repository"].value["name"] == "[REDACTED]"


def test_links_are_immutable_idempotent_and_transactions_roll_back(database):
    _, engine = database
    original = synthetic()
    run_id, other_id = str(uuid4()), str(uuid4())
    sink = PostgresSink(engine)
    sink.persist(payload(original), {"run_id": run_id})
    digest = sink.last_snapshot_id
    sink.persist(payload(original), {"run_id": run_id.upper()})
    assert sink.last_snapshot_id == digest
    sink.persist(payload(original), {"run_id": other_id})
    changed = copy.deepcopy(original)
    changed.projects[0].fields.pop("policy_types")
    changed.window = Window(WINDOW.start, WINDOW.end + timedelta(days=1), WINDOW.as_of + timedelta(days=1))
    with pytest.raises(ValueError, match="already linked"):
        sink.persist({**payload(changed), "raw_ado_data": {"replaced": True}}, {"run_id": run_id})
    with engine.connect() as conn:
        assert count(conn, snapshots) == 1 and count(conn, run_evidence) == 2
        assert get_run_snapshot_id(conn, run_id) == digest
        assert conn.scalar(sa.select(RawMetric.raw_data).where(RawMetric.run_id == run_id)) == {"evidence": "original"}
    with pytest.raises(RuntimeError, match="rollback"), engine.begin() as conn:
        PostgresSink(conn).persist(payload(changed), {"run_id": str(uuid4())})
        assert count(conn, snapshots) == 2 and count(conn, run_evidence) == 3
        assert count(conn, RawMetric.__table__) == 3 and count(conn, Metric.__table__) == 3
        raise RuntimeError("rollback")
    with engine.connect() as conn:
        assert count(conn, snapshots) == 1 and count(conn, run_evidence) == 2
        assert count(conn, Run.__table__) == 2 and count(conn, RawMetric.__table__) == 2
        assert count(conn, Metric.__table__) == 2
        assert all(row.snapshot_id == digest for row in conn.execute(sa.select(checkpoints)))
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(sa.delete(Run).where(Run.id == run_id))


@pytest.mark.parametrize('write_files', [False, True])
def test_standalone_conflict_preserves_linked_run_and_reports(database, tmp_path, write_files):
    url, engine = database
    settings = Settings(database_url=url, azure_devops_org='example', azure_devops_pat='dummy-secret',
                        exemptions_file=None, exports_dir=tmp_path / 'reports')
    config = CollectionConfig(connection_id='syn', project='Alpha', write_db=True, db_strict=True,
                              write_files=write_files, include_maturity=False, run_label='original')
    run_id = str(uuid4())
    first = asyncio.run(execute_collection_run(config, settings, run_id, connection_provider='synthetic'))
    assert first['status'] == 'succeeded', first
    with engine.connect() as conn:
        before = dict(conn.execute(sa.select(Run.__table__).where(Run.id == run_id)).mappings().one())
    files = {path: path.read_bytes() for path in settings.exports_dir.rglob('*') if path.is_file()}
    replacement = config.model_copy(update={'project': 'Beta', 'run_label': 'replacement'})
    rejected = asyncio.run(execute_collection_run(replacement, settings, run_id, connection_provider='synthetic'))
    assert rejected['status'] == 'failed', rejected
    with engine.connect() as conn:
        after = dict(conn.execute(sa.select(Run.__table__).where(Run.id == run_id)).mappings().one())
        assert after == before
        assert get_run_snapshot_id(conn, run_id) == first['snapshot_id']
        assert load_snapshot(conn, first['snapshot_id']).projects[0].identity.project == 'p1'
    assert {path: path.read_bytes() for path in settings.exports_dir.rglob('*') if path.is_file()} == files
    repeated = asyncio.run(execute_collection_run(config, settings, run_id, connection_provider='synthetic'))
    assert repeated['status'] == 'succeeded' and repeated['snapshot_id'] == first['snapshot_id']


@pytest.mark.parametrize("fence", ["cancel", "stale"])
def test_worker_fence_prevents_evidence_and_raw_writes(database, fence):
    url, engine = database
    settings = Settings(database_url=url, azure_devops_org="example", azure_devops_pat="dummy-secret",
                        exemptions_file=None)
    run_id = queue.enqueue(engine, CollectionConfig(project="Alpha", write_files=False), settings)
    original = synthetic()

    async def executor(config, settings, run_id, persist_result):
        if fence == "cancel":
            assert queue.cancel(engine, run_id) == "cancelling"
        else:
            with engine.begin() as conn:
                conn.execute(sa.update(Job).where(Job.run_id == run_id).values(
                    lease_until=queue.now() - timedelta(seconds=1)))
            assert queue.claim(engine, 60)
        result = {"status": "succeeded", "outputs": {}}
        persist_result(payload(original), {"run_id": run_id}, result)
        return result

    assert asyncio.run(run_once(engine, settings, executor=executor))
    with engine.connect() as conn:
        assert count(conn, snapshots) == count(conn, run_evidence) == count(conn, RawMetric.__table__) == 0
        assert count(conn, checkpoints) == 0
        job = conn.execute(sa.select(Job.status, Job.result).where(Job.run_id == run_id)).one()
        assert job.status == ("cancelled" if fence == "cancel" else "running")
        assert "snapshot_id" not in (job.result or {})


def test_load_rejects_tampering_and_existing_id_corruption(database):
    _, engine = database
    original = synthetic()
    sink = PostgresSink(engine)
    sink.persist(payload(original), {"run_id": str(uuid4())})
    digest = sink.last_snapshot_id
    with engine.begin() as conn:
        data = conn.scalar(sa.select(snapshots.c.payload).where(snapshots.c.id == digest))
        data["projects"][0]["fields"]["project"]["detail"] = "private non-scoring change"
        conn.execute(sa.update(snapshots).where(snapshots.c.id == digest).values(payload=data))
    with engine.begin() as conn:
        with pytest.raises(ValueError, match="does not match") as error:
            load_snapshot(conn, digest)
        assert "private" not in str(error.value)
    with pytest.raises(ValueError, match="does not match"):
        sink.persist(payload(original), {"run_id": str(uuid4())})
    with engine.connect() as conn:
        assert count(conn, Run.__table__) == 1


def test_old_format_one_and_malformed_payloads(database):
    _, engine = database
    data = {"format_version": 1, "connection": "old", "window": {
        "start": "2026-01-01T00:00:00.000000+00:00", "end": "2026-02-01T00:00:00.000000+00:00",
        "as_of": "2026-02-02T00:00:00.000000+00:00"}, "capabilities": [],
        "projects": [], "repositories": [], "builds": []}
    def insert(conn, obj):
        encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        conn.execute(snapshots.insert().values(id=digest, connection_id="old", window_start="start",
                     window_end="end", as_of="asof", format_version=1, payload=obj))
        return digest
    with engine.begin() as conn:
        old_id = insert(conn, data)
        assert load_snapshot(conn, old_id).window == WINDOW
        assert load_snapshot(conn, old_id).collection_started_at is None
        malformed = copy.deepcopy(data)
        malformed["window"]["start"] = "sensitive-not-a-date"
        malformed_id = insert(conn, malformed)
        with pytest.raises(ValueError, match="Malformed evidence") as error:
            load_snapshot(conn, malformed_id)
        assert "sensitive" not in str(error.value)


def test_migration_from_0005_and_read_only_missing_status(tmp_path):
    missing = tmp_path / "missing #space.db"
    url = f"sqlite:///{missing}"
    assert status(url) is None and not missing.exists()
    engine = init_engine_and_tables(url)
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        for _, apply, _ in REVISIONS[:5]:
            apply(op)
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('plumbline_0005')"))
        run_id = str(uuid4())
        now = datetime.now(timezone.utc)
        conn.execute(sa.text("INSERT INTO runs (id, executed_at, status) VALUES (:id, :time, 'succeeded')"),
                     {"id": run_id, "time": now})
        conn.execute(sa.insert(Job.__table__).values(id=str(uuid4()), run_id=run_id, config={},
                     status="succeeded", attempts=1, created_at=now, result={"snapshot_id": "old-id"}))
    assert status(url) == "plumbline_0005"
    assert migrate(url) == HEAD == "plumbline_0008"
    assert status(url) == HEAD
    with engine.connect() as conn:
        assert count(conn, Run.__table__) == 1 and count(conn, run_evidence) == 0
        assert conn.scalar(sa.select(Job.result))["snapshot_id"] == "old-id"
        assert {fk["options"].get("ondelete") for fk in sa.inspect(conn).get_foreign_keys("run_evidence")} == {"RESTRICT"}


@pytest.mark.parametrize('uri', ['true', '1', 'yes'])
def test_explicit_sqlite_uri_status_cannot_create_or_write(tmp_path, uri):
    from sqlalchemy.engine import URL

    path = tmp_path / 'uri #space.db'
    # URL.create preserves both SQLAlchemy's escaping and SQLite's file-URI escaping.
    url = str(URL.create('sqlite', database=path.as_uri(), query={'mode': 'rwc', 'uri': uri}))
    assert status(url) is None
    assert not path.exists()
    migrate(f'sqlite:///{path}')
    before = path.read_bytes()
    assert status(url) == HEAD
    assert path.read_bytes() == before


@pytest.mark.parametrize('url', [
    'sqlite:///not_a_uri?mode=memory&uri=true',
    'sqlite:///file:?uri=true',
    'sqlite:///file:memory?mode=memory&mode=rwc&uri=true',
    'sqlite:///file:memory?uri=true&uri=true',
    'sqlite:///file:memory?uri=invalid',
    'sqlite:///?mode=memory&uri=true',
    'sqlite:///?mode=rwc&uri=true',
    'sqlite:///?uri=true',
    'sqlite://?uri=true',
    'sqlite:///file:memory?cache=shared&cache=private&mode=memory&uri=true',
    'sqlite:///file:memory?timeout=5&timeout=10&mode=memory&uri=true',
])
def test_invalid_sqlite_uri_status_never_creates_files(tmp_path, monkeypatch, url):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        status(url)
    assert list(tmp_path.iterdir()) == []


def test_postgresql_run_evidence_ddl_compiles():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    upgrade(Operations(context))
    ddl = output.getvalue()
    assert "CREATE TABLE run_evidence" in ddl
    assert "REFERENCES runs (id) ON DELETE RESTRICT" in ddl
    assert "REFERENCES evidence_snapshots (id) ON DELETE RESTRICT" in ddl
