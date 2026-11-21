"""Worker process for durable collection jobs."""
import asyncio
import logging
import random
import sqlite3
import threading

from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError, SQLAlchemyError, TimeoutError as PoolTimeoutError

from plumbline.api.models import CollectionConfig
from plumbline.jobs import queue
from plumbline.persistence.sinks import FileSink, PostgresSink
from plumbline.runner import execute_collection_run

logger = logging.getLogger(__name__)

_MAX_POLL_RETRIES = 5
_RETRY_BASE_DELAY = 0.5
_MAX_RETRY_DELAY = 8.0


def _transient_database_error(exc):
    """Retry known connection/lock failures, not schema or configuration errors."""
    if isinstance(exc, PoolTimeoutError):
        return True
    if not isinstance(exc, DBAPIError) or isinstance(exc, ProgrammingError):
        return False
    original = exc.orig
    sqlite_code = getattr(original, "sqlite_errorcode", None)
    if sqlite_code is not None:
        return sqlite_code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate:
        return sqlstate in {
            "08000", "08001", "08003", "08006", "08007",  # connection failures
            "40001", "40P01", "55P03",  # transaction or lock contention
            "53300", "57P01", "57P02", "57P03",  # capacity or server restart
        }
    message = str(original).lower()
    # libpq can combine multiple connection attempts in one error message.
    if any(permanent in message for permanent in (
        "authentication failed", "password authentication failed", "no pg_hba.conf entry",
        "role does not exist", "database does not exist",
    )):
        return False
    if exc.connection_invalidated:
        return True
    # libpq can report failed connection attempts without a SQLSTATE. Do not retry
    # arbitrary OperationalError messages such as bad credentials or missing schemas.
    return isinstance(exc, OperationalError) and any(
        transient in message
        for transient in ("connection refused", "connection timed out", "connection reset by peer",
                          "timeout expired", "network is unreachable", "no route to host",
                          "temporary failure in name resolution", "server closed the connection unexpectedly")
    )


def _retry_delay(attempt):
    ceiling = min(_MAX_RETRY_DELAY, _RETRY_BASE_DELAY * 2 ** (attempt - 1))
    return random.uniform(ceiling / 2, ceiling)


async def run_once(engine, settings, *, lease_seconds=60, executor=execute_collection_run):
    queue.recover_cancelled(engine)
    claimed = queue.claim(engine, lease_seconds)
    if not claimed:
        return False
    run_id, owner, data = claimed
    try:
        config = CollectionConfig.model_validate(data)
    except Exception as exc:
        await asyncio.to_thread(queue.finish, engine, run_id, owner, "failed", error=f"Invalid job configuration: {exc}")
        return True
    # Job runs always retain their database snapshot, regardless of the file option.
    config = config.model_copy(update={"write_db": True, "db_strict": True})

    credential = getattr(settings, "azure_devops_pat", None)
    secret = credential.get_secret_value() if credential is not None else None

    def persist(payload, context, result):
        if secret:
            from plumbline.connections.execution import redact
            payload = redact(payload, secret)
        def snapshot(conn):
            outputs = {}
            if config.write_files:
                outputs = FileSink().persist(payload, context) or {}
            sink = PostgresSink(conn)
            sink.persist(payload, context)
            if sink.last_snapshot_id is not None:
                result["snapshot_id"] = sink.last_snapshot_id
            result["outputs"] = outputs
        serial = {key: result.get(key) for key in ("outputs", "artifacts_dir", "completeness", "assessment_status", "assessment_error")}
        if secret:
            from plumbline.connections.execution import redact
            serial = redact(serial, secret)
        # Snapshot may populate outputs, so build the serial form after it runs.
        def save(conn):
            snapshot(conn)
            serial["outputs"] = result["outputs"]
            if "snapshot_id" in result:
                serial["snapshot_id"] = result["snapshot_id"]
        if not queue.finish(engine, run_id, owner, "succeeded", serial, persist=save):
            raise RuntimeError("Worker lease lost before snapshot persistence")

    def progress(item):
        if secret:
            from plumbline.connections.execution import redact
            item = redact(item, secret)
        if not queue.progress(engine, run_id, owner, item):
            raise asyncio.CancelledError("Worker lease lost or cancellation requested")

    async def execute():
        nonlocal secret
        run_config, run_settings, provider = config, settings, None
        if config.connection_id:
            from plumbline.connections.execution import resolve
            run_config, run_settings, provider = await asyncio.to_thread(resolve, engine, config, settings)
        kwargs = {"persist_result": persist}
        if executor is execute_collection_run:
            kwargs.update(attempt_id=owner, progress_callback=progress, connection_provider=provider)
        if provider == "ado" or (provider is None and executor is execute_collection_run):
            from plumbline.connections.execution import protect_logs
            secret = run_settings.azure_devops_pat.get_secret_value()
            with protect_logs(secret):
                return await executor(run_config, run_settings, run_id, **kwargs)
        return await executor(run_config, run_settings, run_id, **kwargs)

    task = asyncio.create_task(execute())
    loop = asyncio.get_running_loop()
    stop = threading.Event()
    lease_state = {"valid": True, "cancelled": False}

    def renew():
        while not stop.wait(max(0.01, lease_seconds / 3)):
            try:
                valid, cancelled = queue.heartbeat(engine, run_id, owner, lease_seconds)
            except Exception:
                logger.exception("Heartbeat failed for job %s", run_id)
                valid, cancelled = False, False
            if not valid or cancelled:
                lease_state.update(valid=valid, cancelled=cancelled)
                loop.call_soon_threadsafe(task.cancel)
                return

    heartbeat = threading.Thread(target=renew, name=f"job-heartbeat-{run_id}", daemon=True)
    heartbeat.start()
    try:
        result = await task
        if result.get("status") != "succeeded":
            from plumbline.connections.execution import redact
            error = result.get("error") if result.get("error") in {"Invalid exemptions configuration", "Database operation failed"} or not config.connection_id else "Connection collection failed"
            await asyncio.to_thread(queue.finish, engine, run_id, owner, "failed", error=redact(error, secret))
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if asyncio.current_task().cancelling():
            await asyncio.to_thread(queue.release, engine, run_id, owner)
            raise
        if lease_state["valid"]:
            await asyncio.to_thread(queue.finish, engine, run_id, owner, "cancelled")
    except Exception as exc:
        if isinstance(exc, SQLAlchemyError):
            error = "Database operation failed"
            logger.error("Job %s failed: %s", run_id, error)
        elif config.connection_id:
            from plumbline.connections.execution import ConnectionValidationError
            error = str(exc) if isinstance(exc, ConnectionValidationError) else "Connection collection failed"
            logger.error("Job %s failed: %s", run_id, error)
        else:
            from plumbline.connections.execution import redact
            error = redact(str(exc), secret)
            logger.error("Job %s failed: %s", run_id, error)
        await asyncio.to_thread(queue.finish, engine, run_id, owner, "failed", error=error)
    finally:
        stop.set()
        await asyncio.to_thread(heartbeat.join)
    return True


async def serve(engine, settings, poll_seconds=2, *, startup_check=None):
    retries = 0
    started = False
    while True:
        try:
            if not started:
                if startup_check is not None:
                    startup_check()
                started = True
            processed = await run_once(engine, settings)
        except (DBAPIError, PoolTimeoutError) as exc:
            if asyncio.current_task().cancelling():
                logger.error("Database operation failed during worker shutdown; any unreleased lease must expire")
                raise asyncio.CancelledError from exc
            if not _transient_database_error(exc) or retries >= _MAX_POLL_RETRIES:
                raise
            retries += 1
            delay = _retry_delay(retries)
            # Driver messages and SQL may contain credentials or job data.
            logger.warning("Transient database failure; retry %s/%s in %.2fs",
                           retries, _MAX_POLL_RETRIES, delay)
            await asyncio.sleep(delay)
            continue
        retries = 0
        if not processed:
            await asyncio.sleep(poll_seconds)
