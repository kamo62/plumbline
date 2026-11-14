"""Read-only connection diagnostics for the provider boundary."""
import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import typer

from plumbline.providers import ADOProvider, SyntheticProvider, Window, doctor
from plumbline.providers.synthetic import WINDOW


def register(app: typer.Typer) -> None:
    @app.command("doctor")
    def doctor_command(
        provider: str = typer.Option("ado", "--provider", help="ado or synthetic"),
        projects: Optional[list[str]] = typer.Option(None, "--project"),
        start: Optional[datetime] = typer.Option(None, "--start"),
        end: Optional[datetime] = typer.Option(None, "--end"),
        as_of: Optional[datetime] = typer.Option(None, "--as-of"),
    ) -> None:
        """Check connection evidence without storing responses. All dates require a timezone."""
        if provider not in {"ado", "synthetic"}:
            raise typer.BadParameter("provider must be ado or synthetic")
        if provider == "ado" and (not projects or not all((start, end, as_of))):
            raise typer.BadParameter("ADO requires --project, --start, --end and --as-of")
        try:
            window = Window(start or WINDOW.start, end or WINDOW.end, as_of or WINDOW.as_of)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        async def check():
            if provider == "synthetic":
                return await doctor(SyntheticProvider(), projects or ["Alpha", "Beta"], window)
            from plumbline.backend.api.ado_client import AzureDevOpsClient
            from plumbline.config.settings import Settings
            settings = Settings()
            async with AzureDevOpsClient(settings) as client:
                return await doctor(ADOProvider(client, "ado-diagnostic"), projects or [], window)

        try:
            result = asyncio.run(check())
        except Exception:
            # Do not echo exception messages that may contain transport credentials.
            typer.echo(json.dumps({"error": "diagnostic_failed", "hint": "Check provider settings and the timezone-aware window."}))
            raise typer.Exit(1)
        typer.echo(json.dumps(asdict(result), sort_keys=True))
        if any(state in {"failed", "partial", "unavailable"} for _, state, _, _ in result.checks):
            raise typer.Exit(1)
