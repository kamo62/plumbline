import asyncio
import os
import select as io_select
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session

from plumbline.api.models import CollectionConfig
from plumbline.jobs import queue, worker
from plumbline.jobs.models import Job
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import RawMetric, init_engine_and_tables


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    migrate(url)
    return url, init_engine_and_tables(url)


def busy_error():
    original = sqlite3.OperationalError("database is locked")
    original.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return OperationalError("synthetic statement", {}, original)


def test_transient_claim_failure_recovers_and_processes_job(database, monkeypatch):
    _, engine = database
    settings = SimpleNamespace(azure_devops_org="example", run_env="ci")
    run_id = queue.enqueue(engine, CollectionConfig(project="one", write_files=False), settings)
    claim = queue.claim
    calls = []

    def interrupted_claim(*args):
        calls.append(True)
        if len(calls) == 1:
            raise busy_error()
        return claim(*args)

    async def executor(config, settings, run_id, persist_result):
        result = {"status": "succeeded", "artifacts_dir": None}
        persist_result({"raw_ado_data": {"repositories": [{"id": "synthetic"}]}}, {"run_id": run_id}, result)
        return result

    # Keep the actual run_once path while substituting only its synthetic executor.
    run_once = worker.run_once
    async def poll(engine, settings):
        processed = await run_once(engine, settings, executor=executor)
        if processed:
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return processed

    monkeypatch.setattr(queue, "claim", interrupted_claim)
    monkeypatch.setattr(worker, "run_once", poll)
    monkeypatch.setattr(worker, "_retry_delay", lambda count: 0, raising=False)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.serve(engine, settings))

    assert len(calls) == 2
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        raw = session.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one()
        assert job.status == "succeeded" and job.attempts == 1
        assert raw.raw_data["repositories"] == [{"id": "synthetic"}]


def test_retry_budget_exhaustion_and_reset(monkeypatch):
    failures = []
    delays = []
    failure = busy_error()

    async def always_fails(engine, settings):
        failures.append(True)
        raise failure

    monkeypatch.setattr(worker, "run_once", always_fails)
    monkeypatch.setattr(worker, "_retry_delay", lambda count: delays.append(count) or 0)
    with pytest.raises(OperationalError) as caught:
        asyncio.run(worker.serve(None, None))
    assert caught.value is failure
    assert len(failures) == worker._MAX_POLL_RETRIES + 1
    assert delays == list(range(1, worker._MAX_POLL_RETRIES + 1))

    failures.clear()
    delays.clear()
    calls = 0
    async def recover_then_fail(engine, settings):
        nonlocal calls
        calls += 1
        if calls == worker._MAX_POLL_RETRIES + 1:
            return False
        raise failure

    monkeypatch.setattr(worker, "run_once", recover_then_fail)
    with pytest.raises(OperationalError):
        asyncio.run(worker.serve(None, None, poll_seconds=0))
    assert calls == 2 * worker._MAX_POLL_RETRIES + 2
    assert delays == list(range(1, worker._MAX_POLL_RETRIES + 1)) * 2


def test_permanent_schema_failure_surfaces_without_retry(database, monkeypatch):
    _, engine = database
    calls = []
    def broken_claim(*args):
        calls.append(True)
        with engine.connect() as conn:
            conn.execute(text("SELECT * FROM table_that_does_not_exist"))
    monkeypatch.setattr(queue, "claim", broken_claim)
    with pytest.raises(OperationalError, match="no such table"):
        asyncio.run(worker.serve(engine, SimpleNamespace()))
    assert len(calls) == 1


@pytest.mark.parametrize("code,message,transient", [
    ("08006", "connection lost", True),
    ("08004", "connection rejected", False),
    ("53300", "too many connections", True),
    ("57P01", "server shutting down", True),
    ("28P01", "password authentication failed", False),
    ("3D000", "database does not exist", False),
    ("42P01", "undefined table", False),
    (None, "connection refused", True),
    (None, "connection timeout expired", True),
    (None, "temporary failure in name resolution", True),
    (None, "password authentication failed", False),
    (None, "connection refused; password authentication failed", False),
])
def test_postgresql_operational_failure_classification(monkeypatch, code, message, transient):
    class DriverError(Exception):
        sqlstate = code
    error = OperationalError(None, None, DriverError(message))
    calls = []
    async def poll(engine, settings):
        calls.append(True)
        raise error
    monkeypatch.setattr(worker, "run_once", poll)
    monkeypatch.setattr(worker, "_retry_delay", lambda count: 0)
    with pytest.raises(OperationalError):
        asyncio.run(worker.serve(None, None))
    assert len(calls) == (worker._MAX_POLL_RETRIES + 1 if transient else 1)


def test_cancellation_interrupts_backoff(monkeypatch):
    async def exercise():
        entered_backoff = asyncio.Event()
        async def fail(engine, settings):
            raise busy_error()
        def delay(count):
            entered_backoff.set()
            return 60
        monkeypatch.setattr(worker, "run_once", fail)
        monkeypatch.setattr(worker, "_retry_delay", delay)
        task = asyncio.create_task(worker.serve(None, None))
        await asyncio.wait_for(entered_backoff.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    asyncio.run(exercise())


def test_database_failure_during_release_does_not_swallow_shutdown(database, monkeypatch, caplog):
    _, engine = database
    settings = SimpleNamespace(azure_devops_org="example", run_env="ci")
    run_id = queue.enqueue(engine, CollectionConfig(project="one", write_files=False), settings)
    real_run_once = worker.run_once
    attempts = []

    async def exercise():
        started = asyncio.Event()
        async def executor(config, settings, run_id, persist_result):
            started.set()
            await asyncio.Event().wait()
        async def poll(engine, settings):
            attempts.append(True)
            return await real_run_once(engine, settings, executor=executor)
        def release(*args):
            raise busy_error()
        monkeypatch.setattr(worker, "run_once", poll)
        monkeypatch.setattr(queue, "release", release)
        task = asyncio.create_task(worker.serve(engine, settings))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    asyncio.run(exercise())
    assert len(attempts) == 1
    assert "unreleased lease must expire" in caplog.text
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "running" and job.attempts == 1


def test_programming_failure_is_not_retried(monkeypatch):
    calls = []
    async def broken(engine, settings):
        calls.append(True)
        raise ValueError("synthetic programming error")
    monkeypatch.setattr(worker, "run_once", broken)
    with pytest.raises(ValueError, match="synthetic programming error"):
        asyncio.run(worker.serve(None, None))
    assert len(calls) == 1


def test_programming_error_is_not_retried_even_if_connection_invalidated(monkeypatch):
    calls = []
    async def broken(engine, settings):
        calls.append(True)
        raise ProgrammingError(None, None, Exception("synthetic"), connection_invalidated=True)
    monkeypatch.setattr(worker, "run_once", broken)
    with pytest.raises(ProgrammingError):
        asyncio.run(worker.serve(None, None))
    assert len(calls) == 1


def test_cli_restores_signal_handler_after_error_and_cancellation(monkeypatch):
    from plumbline import jobs_cli
    previous = signal.getsignal(signal.SIGTERM)
    async def fail(*args, **kwargs):
        raise ValueError("synthetic failure")
    monkeypatch.setattr(jobs_cli, "serve", fail)
    with pytest.raises(ValueError, match="synthetic failure"):
        asyncio.run(jobs_cli._serve(None, SimpleNamespace(database_url="unused"), 1))
    assert signal.getsignal(signal.SIGTERM) == previous

    async def exercise():
        started = asyncio.Event()
        async def wait(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(jobs_cli, "serve", wait)
        task = asyncio.create_task(jobs_cli._serve(None, SimpleNamespace(database_url="unused"), 1))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert signal.getsignal(signal.SIGTERM) == previous

    async def terminate():
        started = asyncio.Event()
        async def wait(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(jobs_cli, "serve", wait)
        task = asyncio.create_task(jobs_cli._serve(None, SimpleNamespace(database_url="unused"), 1))
        await started.wait()
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(task, timeout=1)
    asyncio.run(terminate())
    assert signal.getsignal(signal.SIGTERM) == previous


def test_startup_migration_check_is_retried_but_outdated_schema_is_not(database, monkeypatch):
    from plumbline import jobs_cli
    url, engine = database
    settings = SimpleNamespace(database_url=url)
    actual_status = jobs_cli.status
    calls = []
    def status_with_outage(url):
        calls.append(True)
        if len(calls) == 1:
            raise busy_error()
        return actual_status(url)
    async def stop(engine, settings):
        raise RuntimeError("reached polling")
    monkeypatch.setattr(jobs_cli, "status", status_with_outage)
    monkeypatch.setattr(worker, "run_once", stop)
    monkeypatch.setattr(worker, "_retry_delay", lambda count: 0)
    with pytest.raises(RuntimeError, match="reached polling"):
        asyncio.run(worker.serve(engine, settings, startup_check=lambda: jobs_cli._check_migrations(url)))
    assert len(calls) == 2
    monkeypatch.setattr(jobs_cli, "status", lambda url: None)
    with pytest.raises(typer.BadParameter, match="migrations are not current"):
        asyncio.run(worker.serve(engine, settings, startup_check=lambda: jobs_cli._check_migrations(url)))


def test_backoff_is_bounded_and_jittered(monkeypatch):
    bounds = []
    def choose(low, high):
        bounds.append((low, high))
        return low
    monkeypatch.setattr(worker.random, "uniform", choose)
    delays = [worker._retry_delay(count) for count in range(1, 20)]
    assert all(0 < low < high <= worker._MAX_RETRY_DELAY for low, high in bounds)
    assert delays == [low for low, _ in bounds]
    assert bounds[-1][1] == worker._MAX_RETRY_DELAY


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_cli_sigterm_releases_running_claim(database, tmp_path):
    url, engine = database
    run_id = queue.enqueue(engine, CollectionConfig(project="one", write_files=False),
                           SimpleNamespace(azure_devops_org="example", run_env="ci"))
    script = '''
import asyncio
import typer
from plumbline.jobs import worker
from plumbline.jobs_cli import register
real_run_once = worker.run_once
async def executor(config, settings, run_id, persist_result):
    print("EXECUTOR_STARTED", flush=True)
    await asyncio.Event().wait()
async def poll(engine, settings):
    return await real_run_once(engine, settings, executor=executor)
worker.run_once = poll
app = typer.Typer()
@app.callback()
def main():
    pass
register(app)
app()
'''
    root = Path(__file__).resolve().parents[2]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "PYTHONPATH": os.pathsep.join((str(root), str(root / "src"))),
           "PLUMBLINE_DATABASE_URL": url, "PLUMBLINE_AZURE_DEVOPS_ORG": "example"}
    process = subprocess.Popen([sys.executable, "-c", script, "worker"], cwd=tmp_path,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = io_select.select([process.stdout], [], [], 10)
        assert ready and process.stdout.readline().strip() == "EXECUTOR_STARTED"
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    with Session(engine) as session:
        job = session.execute(select(Job).where(Job.run_id == run_id)).scalar_one()
        assert job.status == "queued" and job.owner is None and job.attempts == 1
    assert queue.claim(engine) is not None
