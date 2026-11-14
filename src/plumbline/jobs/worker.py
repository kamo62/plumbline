"""Worker process for durable collection jobs."""
import asyncio
import logging
import threading

from plumbline.api.models import CollectionConfig
from plumbline.jobs import queue
from plumbline.persistence.sinks import FileSink, PostgresSink
from plumbline.runner import execute_collection_run

logger = logging.getLogger(__name__)


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

    secret = None

    def persist(payload, context, result):
        if secret:
            from plumbline.connections.execution import redact
            payload = redact(payload, secret)
        def snapshot(conn):
            outputs = {}
            if config.write_files:
                outputs = FileSink().persist(payload, context) or {}
            PostgresSink(conn).persist(payload, context)
            if payload.get("provider_snapshot") is not None:
                from plumbline.collection.store import persist_snapshot
                result["snapshot_id"] = persist_snapshot(conn, payload["provider_snapshot"])
            result["outputs"] = outputs
        serial = {key: result.get(key) for key in ("outputs", "artifacts_dir", "completeness", "assessment_status")}
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
        if provider == "ado":
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
            await asyncio.to_thread(queue.finish, engine, run_id, owner, "failed", error="Connection collection failed" if config.connection_id else result.get("error"))
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
        if config.connection_id:
            from plumbline.connections.execution import ConnectionValidationError
            error = str(exc) if isinstance(exc, ConnectionValidationError) else "Connection collection failed"
            logger.error("Job %s failed: %s", run_id, error)
        else:
            logger.exception("Job %s failed", run_id)
            error = str(exc)
        await asyncio.to_thread(queue.finish, engine, run_id, owner, "failed", error=error)
    finally:
        stop.set()
        await asyncio.to_thread(heartbeat.join)
    return True


async def serve(engine, settings, poll_seconds=2):
    while True:
        if not await run_once(engine, settings):
            await asyncio.sleep(poll_seconds)
