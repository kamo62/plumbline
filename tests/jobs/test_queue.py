import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update
from sqlmodel import Session

from plumbline.api.models import CollectionConfig
from plumbline.jobs import queue
from plumbline.jobs.models import Job
from plumbline.jobs.worker import run_once
from plumbline.persistence import baseline, revision_0002_jobs
from plumbline.persistence.models import Run, init_engine_and_tables


@pytest.fixture
def engine(tmp_path):
    engine = init_engine_and_tables(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        baseline.upgrade(op)
        revision_0002_jobs.upgrade(op)
    return engine


@pytest.fixture
def settings():
    return SimpleNamespace(azure_devops_org="example", run_env="ci")


def add(engine, settings):
    return queue.enqueue(engine, CollectionConfig(projects=["one", "two"], write_files=False), settings)


def test_claim_once_and_fence_stale_owner(engine, settings):
    run_id = add(engine, settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: queue.claim(engine, 60), range(2)))
    claim = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    with Session(engine) as session, session.begin():
        session.execute(update(Job).where(Job.run_id == run_id).values(lease_until=queue.now() - timedelta(seconds=1)))
    replacement = queue.claim(engine, 60)
    assert replacement and replacement[1] != claim[1]
    assert not queue.finish(engine, run_id, claim[1], "failed", error="stale")
    assert queue.finish(engine, run_id, replacement[1], "succeeded", {"completeness": {"complete": False}})
    with Session(engine) as session:
        assert session.execute(select(Run.status).where(Run.id == run_id)).scalar_one() == "succeeded"
        assert session.execute(select(Job.result).where(Job.run_id == run_id)).scalar_one()["completeness"]["complete"] is False


def test_cancel_and_recover_expired(engine, settings):
    run_id = add(engine, settings)
    claimed = queue.claim(engine, 60)
    assert queue.cancel(engine, run_id) == "cancelling"
    assert queue.heartbeat(engine, run_id, claimed[1])[1]
    with Session(engine) as session, session.begin():
        session.execute(update(Job).where(Job.run_id == run_id).values(lease_until=queue.now() - timedelta(seconds=1)))
    queue.recover_cancelled(engine)
    with Session(engine) as session:
        assert session.execute(select(Run.status).where(Run.id == run_id)).scalar_one() == "cancelled"
    assert queue.claim(engine) is None


def test_worker_runs_synthetic_executor(engine, settings):
    run_id = add(engine, settings)
    async def executor(config, settings, run_id, persist_result):
        assert config.projects == ["one", "two"]
        result = {"status": "succeeded", "completeness": {"complete": False}, "assessment_status": "blocked_incomplete_collection", "artifacts_dir": None}
        persist_result({"raw_ado_data": {"repositories": {"one": {}}}}, {"run_id": run_id}, result)
        return result
    assert asyncio.run(run_once(engine, settings, executor=executor))
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "succeeded"
        assert job.result["assessment_status"] == "blocked_incomplete_collection"


def test_api_enqueue_and_restart_visible_without_provider(engine, settings, monkeypatch):
    from fastapi.testclient import TestClient
    from plumbline.api import main
    from plumbline.backend.api.ado_client import AzureDevOpsClient

    monkeypatch.setattr(main, "get_engine", lambda settings: engine)
    monkeypatch.setattr(AzureDevOpsClient, "__init__", lambda *args: pytest.fail("provider called"))
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    try:
        from plumbline.persistence.revision_0003_auth import upgrade as auth_upgrade
        from plumbline.auth.service import bootstrap
        with engine.begin() as conn:
            auth_upgrade(Operations(MigrationContext.configure(conn)))
        settings.database_url = str(engine.url)
        with Session(engine) as db:
            user = bootstrap(db, "admin", "initial-password-123")
            user.force_change = False
            db.add(user)
            db.commit()
        client = TestClient(main.app)
        csrf = client.get("/auth/csrf").json()["csrf_token"]
        login = client.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "admin", "password": "initial-password-123"})
        assert login.status_code == 200
        client.headers["x-csrf-token"] = login.json()["csrf_token"]
        response = client.post("/v1/runs", json={"projects": ["one", "two"], "write_db": False, "write_files": False})
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert client.get(f"/v1/runs/{run_id}").json()["status"] == "queued"
        assert client.get("/v1/runs").json()[0]["id"] == run_id
        assert client.delete(f"/v1/runs/{run_id}/data").status_code == 409
        assert client.post(f"/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
    finally:
        main.app.dependency_overrides.clear()


def test_real_runner_selected_projects_partial_snapshot(engine, monkeypatch):
    from plumbline import runner
    from plumbline.config.settings import Settings
    from plumbline.persistence.models import RawMetric

    settings = Settings(azure_devops_pat="test-pat", azure_devops_org="example", exemptions_file=None)
    selected = []
    class Client:
        def __init__(self, settings):
            pass
        async def close(self):
            pass
    class Collector:
        async def collect_all_projects_raw_data(self, projects):
            selected.extend(projects)
            return {"repositories": [{"id": "r1"}], "completeness": {"complete": False, "failures": [{"project": "two"}]}}
    monkeypatch.setattr(runner, "AzureDevOpsClient", Client)
    monkeypatch.setattr(runner.MetricsServiceFactory, "create_service", lambda *a, **kw: Collector())
    run_id = queue.enqueue(engine, CollectionConfig(projects=["one", "two"], write_files=False), settings)
    assert asyncio.run(run_once(engine, settings))
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        raw = session.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one()
        assert selected == ["one", "two"]
        assert job.status == "succeeded" and job.result["completeness"]["complete"] is False
        assert job.result["assessment_status"] == "blocked_incomplete_collection"
        assert raw.raw_data["repositories"] == [{"id": "r1"}]


def test_running_worker_cooperates_with_cancellation(engine, settings):
    run_id = add(engine, settings)
    async def exercise():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def executor(config, settings, run_id, persist_result):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        task = asyncio.create_task(run_once(engine, settings, lease_seconds=0.6, executor=executor))
        await started.wait()
        assert queue.cancel(engine, run_id) == "cancelling"
        await task
        assert cancelled.is_set()
    asyncio.run(exercise())
    with Session(engine) as session:
        assert session.execute(select(Job.status).where(Job.run_id == run_id)).scalar_one() == "cancelled"


def test_worker_in_separate_process(engine, settings):
    import subprocess
    import sys
    run_id = add(engine, settings)
    script = '''
import asyncio, sys
from types import SimpleNamespace
from plumbline.jobs.worker import run_once
from plumbline.persistence.models import init_engine_and_tables
async def synthetic(config, settings, run_id, persist_result):
    result = {"status": "succeeded", "artifacts_dir": None, "completeness": {"complete": True}}
    persist_result({"raw_ado_data": {"repositories": [{"id": "synthetic"}]}}, {"run_id": run_id}, result)
    return result
asyncio.run(run_once(init_engine_and_tables(sys.argv[1]), SimpleNamespace(), executor=synthetic))
'''
    subprocess.run([sys.executable, "-c", script, str(engine.url)], check=True, timeout=15)
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "succeeded" and job.result["completeness"]["complete"]


def test_claim_sql_compiles_for_postgresql():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy import or_
    statement = update(Job).where(or_(Job.status == "queued", Job.lease_until < queue.now())).values(
        status="running", attempts=Job.attempts + 1).returning(Job.run_id)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "UPDATE jobs" in compiled and "RETURNING jobs.run_id" in compiled


def test_expired_attempts_are_bounded(engine, settings):
    run_id = add(engine, settings)
    for _ in range(3):
        assert queue.claim(engine, 1)
        with Session(engine) as session, session.begin():
            session.execute(update(Job).where(Job.run_id == run_id).values(
                lease_until=queue.now() - timedelta(seconds=1)))
    assert queue.claim(engine, 1) is None
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "failed" and job.attempts == 3
        assert "limit" in job.error


def test_progress_is_fenced_and_keeps_project_identity(engine, settings):
    run_id = add(engine, settings)
    old = queue.claim(engine, 60)
    for project in ("project-1", "project-2"):
        assert queue.progress(engine, run_id, old[1], {"project_id": project,
            "repository_id": "same-id", "status": "failed", "errors": ["synthetic error"]})
    with Session(engine) as session:
        result = session.execute(select(Job.result).where(Job.run_id == run_id)).scalar_one()
        assert len(result["repositories"]) == 2
    with Session(engine) as session, session.begin():
        session.execute(update(Job).where(Job.run_id == run_id).values(
            lease_until=queue.now() - timedelta(seconds=1)))
    new = queue.claim(engine, 60)
    assert not queue.progress(engine, run_id, old[1], {"project_id": "project-1", "repository_id": "same-id"})
    assert new and queue.finish(engine, run_id, new[1], "failed", error="permanent")


def test_heartbeat_continues_while_event_loop_is_blocked(engine, settings):
    import threading
    import time

    run_id = add(engine, settings)
    started = threading.Event()
    observed = []

    async def executor(config, settings, run_id, persist_result):
        started.set()
        time.sleep(0.8)
        return {"status": "failed", "error": "synthetic failure"}

    def contender():
        assert started.wait(3)
        time.sleep(0.5)
        observed.append(queue.claim(engine, 0.3))

    thread = threading.Thread(target=contender)
    thread.start()
    asyncio.run(run_once(engine, settings, lease_seconds=0.3, executor=executor))
    thread.join()
    assert observed == [None]
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "failed" and job.attempts == 1


def test_shutdown_releases_claim_after_awaiting_executor(engine, settings):
    run_id = add(engine, settings)
    async def exercise():
        started = asyncio.Event()
        cleaned = asyncio.Event()
        async def executor(config, settings, run_id, persist_result):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        task = asyncio.create_task(run_once(engine, settings, executor=executor))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
    asyncio.run(exercise())
    with Session(engine) as session:
        assert session.execute(select(Job.status).where(Job.run_id == run_id)).scalar_one() == "queued"
    assert queue.claim(engine)


def test_stale_real_runner_cannot_write_attempt_files(engine, tmp_path, monkeypatch):
    from plumbline import runner
    from plumbline.config.settings import Settings

    settings = Settings(azure_devops_org="example", azure_devops_pat="synthetic", exemptions_file=None)
    run_id = queue.enqueue(engine, CollectionConfig(project="one", write_files=True, include_maturity=False,
        output_path=str(tmp_path / "metrics")), settings)
    class Client:
        def __init__(self, settings):
            pass
        async def close(self):
            pass
    class Collector:
        async def collect_all_raw_data(self, project):
            with Session(engine) as session, session.begin():
                session.execute(update(Job).where(Job.run_id == run_id).values(
                    lease_until=queue.now() - timedelta(seconds=1)))
            assert queue.claim(engine, 60)
            return {"repositories": [{"id": "synthetic"}], "completeness": {"complete": True}}
    monkeypatch.setattr(runner, "AzureDevOpsClient", Client)
    monkeypatch.setattr(runner.MetricsServiceFactory, "create_service", lambda *a, **kw: Collector())
    asyncio.run(run_once(engine, settings))
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.rglob("*.xlsx"))
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "running" and job.attempts == 2


def test_real_collector_reports_repository_error_to_job(engine, settings):
    from unittest.mock import AsyncMock, Mock
    from plumbline.backend.services.raw_data_collector import RawDataCollector

    run_id = add(engine, settings)
    claim = queue.claim(engine)
    client = Mock()
    client.get_project = AsyncMock(return_value={"id": "project-1", "name": "one"})
    client.get_repositories = AsyncMock(return_value=[{"id": "repo-1", "name": "repo"}])
    client.get_commits = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    for name in ("get_branches", "get_pull_requests", "get_policy_configurations", "get_policy_types", "get_builds"):
        setattr(client, name, AsyncMock(return_value=[]))
    collector = RawDataCollector(client)
    collector.progress_callback = lambda item: queue.progress(engine, run_id, claim[1], item)
    data = asyncio.run(collector.collect_all_raw_data("one"))
    with Session(engine) as session:
        result = session.execute(select(Job.result).where(Job.run_id == run_id)).scalar_one()
    item = result["repositories"]["project-1:repo-1"]
    assert item["status"] == "failed" and item["errors"][0]["error"] == "synthetic failure"
    assert data["completeness"]["complete"] is False


def test_slow_file_export_holds_fence_against_reclaim(engine, tmp_path, monkeypatch):
    import threading
    import time
    from plumbline import runner
    from plumbline.config.settings import Settings
    from plumbline.jobs import worker

    settings = Settings(azure_devops_org="example", azure_devops_pat="synthetic", exemptions_file=None)
    run_id = queue.enqueue(engine, CollectionConfig(project="one", write_files=True, include_maturity=False,
        output_path=str(tmp_path / "metrics")), settings)
    started = threading.Event()
    claims = []
    class Client:
        def __init__(self, settings):
            pass
        async def close(self):
            pass
    class Collector:
        async def collect_all_raw_data(self, project):
            return {"repositories": [{"id": "synthetic"}], "completeness": {"complete": True}}
    class SlowFileSink:
        def persist(self, payload, context):
            from pathlib import Path
            started.set()
            time.sleep(0.6)
            path = Path(payload["file_output_base"] + ".json")
            path.write_text("synthetic")
            return {"raw_json": str(path)}
    monkeypatch.setattr(runner, "AzureDevOpsClient", Client)
    monkeypatch.setattr(runner.MetricsServiceFactory, "create_service", lambda *a, **kw: Collector())
    monkeypatch.setattr(worker, "FileSink", SlowFileSink)
    def contender():
        assert started.wait(3)
        time.sleep(0.3)
        claims.append(queue.claim(engine, 0.2))
    thread = threading.Thread(target=contender)
    thread.start()
    asyncio.run(run_once(engine, settings, lease_seconds=0.2))
    thread.join()
    assert claims == [None]
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "succeeded" and job.attempts == 1
        assert job.owner in job.result["outputs"]["raw_json"]
