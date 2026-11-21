"""Register the separate worker command with the main CLI."""
import asyncio
import signal
import typer

from plumbline.config.settings import Settings
from plumbline.jobs.worker import serve
from plumbline.persistence.models import init_engine_and_tables
from plumbline.persistence.migrations import HEAD, status


def _check_migrations(url):
    if status(url) != HEAD:
        raise typer.BadParameter("Database migrations are not current; run plumbline migrate")


async def _serve(engine, settings, poll_seconds):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            loop.call_soon_threadsafe(task.cancel)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        try:
            await serve(engine, settings, poll_seconds, startup_check=lambda: _check_migrations(settings.database_url))
        except asyncio.CancelledError:
            if not stopping:
                raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def register(app):
    @app.command("worker")
    def worker(poll_seconds: float = typer.Option(2.0, min=0.1)):
        settings = Settings()
        if not settings.database_url:
            raise typer.BadParameter("Database URL is required")
        asyncio.run(_serve(init_engine_and_tables(settings.database_url), settings, poll_seconds))
