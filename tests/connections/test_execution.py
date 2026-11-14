import asyncio
import json

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlmodel import Session

from plumbline.api.models import CollectionConfig
from plumbline.config.settings import Settings
from plumbline.connections.execution import ConnectionValidationError
from plumbline.connections.models import Connection
from plumbline.connections.service import encrypt
from plumbline.jobs import queue
from plumbline.jobs.models import Job
from plumbline.jobs.worker import run_once
from plumbline.persistence import baseline, revision_0002_jobs, revision_0004_connections, revision_0005_evidence
from plumbline.persistence.models import RawMetric, init_engine_and_tables


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = init_engine_and_tables(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        for migration in (baseline, revision_0002_jobs, revision_0004_connections, revision_0005_evidence):
            migration.upgrade(op)
    settings = Settings(azure_devops_org="server", azure_devops_pat="server-secret", exemptions_file=None)
    with Session(engine) as db:
        db.add(Connection(id="ado", provider="ado", name="ADO", organisation="saved", config={"projects": ["Alpha"]}, credential_ciphertext=encrypt("saved-secret"), capabilities=[]))
        db.add(Connection(id="syn", provider="synthetic", name="Demo", config={"projects": ["Alpha", "Beta"]}, capabilities=[]))
        db.commit()
    return engine, settings


def job(engine, run_id):
    with Session(engine) as db:
        return db.execute(select(Job).where(Job.run_id == run_id)).scalar_one()


def test_enqueue_validation_and_scope(setup):
    engine, settings = setup
    for config, message in [
        (CollectionConfig(connection_id="missing", project="Alpha"), "Connection not found"),
        (CollectionConfig(connection_id="ado", org="other", project="Alpha"), "organisation"),
        (CollectionConfig(connection_id="ado", project="Beta"), "outside connection scope"),
    ]:
        with pytest.raises(ConnectionValidationError, match=message):
            queue.enqueue(engine, config, settings)
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", all_projects=True), settings)
    assert job(engine, run_id).config["projects"] == ["Alpha"]
    assert job(engine, run_id).config["org"] == "saved"


def test_synthetic_real_runner_files_db_and_snapshot(setup, tmp_path):
    engine, settings = setup
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="syn", all_projects=True, output_path=str(tmp_path / "metrics")), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "succeeded", row.error
    assert row.result["completeness"]["complete"] is False
    assert row.result["assessment_status"] == "blocked_incomplete_collection"
    assert row.result["snapshot_id"]
    with Session(engine) as db:
        raw = db.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one().raw_data
        states = [r["fields"]["tests"]["state"] for r in raw["evidence"]["builds"]]
        assert states == ["partial", "failed"]
    from plumbline.collection.store import load_snapshot
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, row.result["snapshot_id"])
        assert len(snapshot.repositories) == 3
        assert snapshot.builds[1].fields["tests"].state.value == "failed"
    files = list(tmp_path.rglob("*.json"))
    assert files
    document = json.loads(files[0].read_text())
    assert document["evidence"]["builds"][0]["fields"]["tests"]["state"] == "partial"
    assert not list(tmp_path.rglob("*maturity*"))


def test_deleted_and_undecryptable_connection_fail_safely(setup, monkeypatch):
    engine, settings = setup
    first = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert asyncio.run(run_once(engine, settings))
    assert job(engine, first).error == "Connection credential is unavailable"
    second = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)
    with Session(engine) as db:
        db.delete(db.get(Connection, "ado"))
        db.commit()
    assert asyncio.run(run_once(engine, settings))
    assert job(engine, second).error == "Connection not found"
    assert "secret" not in json.dumps(job(engine, first).config) + str(job(engine, first).result) + job(engine, first).error


def test_saved_ado_real_runner_mock_transport(setup, monkeypatch, caplog):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    engine, settings = setup
    calls = []
    async def request(client, url, session, params=None, **kwargs):
        calls.append((client.base_url, client.settings.azure_devops_pat.get_secret_value(), url))
        if url.endswith("/_apis/projects"):
            return {"value": [{"id": "p1", "name": "Alpha"}]}, {}
        if url.endswith("/_apis/projects/Alpha"):
            return {"id": "p1", "name": "Alpha"}, {}
        return {"value": []}, {}
    monkeypatch.setattr(AzureDevOpsClient, "_request_json", request)
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", all_projects=True, write_files=False, include_maturity=False), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "succeeded", row.error
    assert calls and all(org == "https://dev.azure.com/saved" and pat == "saved-secret" for org, pat, _ in calls)
    assert "saved-secret" not in json.dumps(row.config) + json.dumps(row.result) + str(row.error) + caplog.text


def test_unsupported_provider_and_secret_in_failure(setup, monkeypatch, caplog):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    engine, settings = setup
    with Session(engine) as db:
        db.add(Connection(id="other", provider="unknown", name="Other", config={}, capabilities=[]))
        db.commit()
    with pytest.raises(ConnectionValidationError, match="unsupported"):
        queue.enqueue(engine, CollectionConfig(connection_id="other", project="Alpha"), settings)

    async def request(client, url, session, params=None, **kwargs):
        raise RuntimeError("transport leaked saved-secret")
    monkeypatch.setattr(AzureDevOpsClient, "_request_json", request)
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "failed"
    assert "saved-secret" not in json.dumps(row.config) + json.dumps(row.result) + str(row.error) + caplog.text


def test_partial_evidence_is_redacted_before_storage(setup):
    engine, settings = setup
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)

    async def executor(config, settings, run_id, persist_result):
        result = {"status": "succeeded", "completeness": {"complete": False}, "outputs": {}}
        persist_result({"raw_ado_data": {"failure": "upstream echoed saved-secret"}},
                       {"run_id": run_id}, result)
        return result

    assert asyncio.run(run_once(engine, settings, executor=executor))
    with Session(engine) as db:
        raw = db.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one().raw_data
        assert raw['failure'] == 'upstream echoed [REDACTED]'
