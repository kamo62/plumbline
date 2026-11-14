"""Register the separate worker command with the main CLI."""
import asyncio
import typer

from plumbline.config.settings import Settings
from plumbline.jobs.worker import serve
from plumbline.persistence.models import init_engine_and_tables
from plumbline.persistence.migrations import HEAD, status


def register(app):
    @app.command("worker")
    def worker(poll_seconds: float = typer.Option(2.0, min=0.1)):
        settings = Settings()
        if not settings.database_url:
            raise typer.BadParameter("Database URL is required")
        if status(settings.database_url) != HEAD:
            raise typer.BadParameter("Database migrations are not current; run plumbline migrate")
        asyncio.run(serve(init_engine_and_tables(settings.database_url), settings, poll_seconds))
