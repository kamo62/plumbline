"""Azure DevOps Metrics CLI

This module provides the command-line interface for collecting and analyzing
Azure DevOps metrics.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from pydantic import SecretStr, ValidationError

from plumbline.config.settings import Settings
from plumbline.backend.api.ado_client import AzureDevOpsClient
from plumbline.utils.logging import configure_logging, get_logger, log_with_context
from plumbline.api.models import CollectionConfig
from plumbline.runner import execute_collection_run

# Initialize logger
logger = get_logger(__name__)

# Initialize console
console = Console()

# Create Typer app
app = typer.Typer(
    name="plumbline",
    help="Plumbline engineering maturity reporting",
    add_completion=False,
)

@app.callback()
def main() -> None:
    configure_logging()


# Verbosity from PLUMBLINE_VERBOSITY (quiet|normal|debug)
_VERBOSITY = os.environ.get("PLUMBLINE_VERBOSITY", "normal").lower()

async def _list_projects(ado_client: AzureDevOpsClient) -> Table:
    """List all Azure DevOps projects.

    Args:
        ado_client: Azure DevOps client instance

    Returns:
        Rich table containing project information
    """
    projects = await ado_client.get_projects()

    # Create projects table
    table = Table(title="Azure DevOps Projects")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="green")
    table.add_column("Last Updated", style="blue")

    for project in projects:
        last_update = project.get("lastUpdateTime", "")
        if last_update:
            try:
                last_update = datetime.fromisoformat(last_update.replace("Z", "+00:00")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (ValueError, AttributeError):
                last_update = "N/A"

        table.add_row(
            project.get("name", "Unknown"),
            project.get("description", "No description"),
            last_update or "N/A",
        )

    return table


async def list_projects_and_cleanup(ado_client: AzureDevOpsClient) -> Table:
    """List projects and ensure client cleanup.

    Args:
        ado_client: Azure DevOps client instance

    Returns:
        Rich table containing project information
    """
    try:
        table = await _list_projects(ado_client)
        return table
    finally:
        await ado_client.close()


@app.command()
def projects() -> None:
    """List all available Azure DevOps projects."""
    try:
        # Load settings
        settings = Settings(
            azure_devops_pat=SecretStr(os.getenv("PLUMBLINE_AZURE_DEVOPS_PAT", "")),
            azure_devops_org=os.getenv("PLUMBLINE_AZURE_DEVOPS_ORG", "")
        )

        # Check for required PAT and Org
        pat = os.getenv("PLUMBLINE_AZURE_DEVOPS_PAT")
        org = os.getenv("PLUMBLINE_AZURE_DEVOPS_ORG")
        if not pat or not org:
            console.print("\n❌ [red]Missing required environment variables.[/]\n")
            console.print("Set PLUMBLINE_AZURE_DEVOPS_PAT and PLUMBLINE_AZURE_DEVOPS_ORG before running this command.")
            raise typer.Exit(code=2)


        # Initialize Azure DevOps client
        ado_client = AzureDevOpsClient(settings)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(description="Fetching projects...", total=None)
            table = asyncio.run(list_projects_and_cleanup(ado_client))

        console.print(table)

    except Exception as e:
        log_with_context(logger, logging.ERROR, f"Error listing projects: {str(e)}", error=str(e))
        console.print(f"\n❌ [red]Error: {str(e)}[/]")
        raise typer.Exit(code=1)


@app.command("db-migrate", help="Apply packaged database migrations")
@app.command("migrate", help="Apply packaged database migrations")
def db_migrate(
    check_only: bool = typer.Option(False, "--check", help="Check without migrating: exit 0 if current, 2 if pending"),
) -> None:
    from plumbline.persistence.migrations import HEAD, migrate, status
    try:
        url = Settings().database_url
        if not url:
            raise ValueError("PLUMBLINE_DATABASE_URL must not be empty")
        if check_only:
            current = status(url)
            console.print(f"Current: {current or 'None'} | Head: {HEAD}")
            raise typer.Exit(code=0 if current == HEAD else 2)
        console.print(f"Migration current: {migrate(url)}")
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"Migration failed: {exc}")
        raise typer.Exit(code=1)


@app.command("db-status", help="Show database migration status")
def db_status() -> None:
    from plumbline.persistence.migrations import HEAD, status
    try:
        url = Settings().database_url
        if not url:
            raise ValueError("PLUMBLINE_DATABASE_URL must not be empty")
        current = status(url)
        console.print(f"Current: {current or 'None'} | Head: {HEAD}")
        if not current:
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"Migration status failed: {exc}")
        raise typer.Exit(code=1)

@app.command(help="Collect raw data from Azure DevOps with optional maturity assessment.")
def collect_raw(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    all_projects: bool = typer.Option(False, "--all-projects", "-a"),
    selected_projects: Optional[list[str]] = typer.Option(None, "--projects", help="Selected project; repeat for multiple projects in one run"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output base inside the run directory"),
    include_maturity: bool = typer.Option(False, "--maturity", "-m"),
    write_files: bool = typer.Option(True, "--write-files/--no-write-files"),
    write_db: bool = typer.Option(False, "--write-db"),
    db_strict: bool = typer.Option(False, "--db-strict"),
    run_label: Optional[str] = typer.Option(None, "--run-label"),
    org: Optional[str] = typer.Option(None, "--org"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    branch: Optional[str] = typer.Option(None, "--branch"),
    commit_sha: Optional[str] = typer.Option(None, "--commit-sha"),
) -> None:
    """Run collection using the same orchestration as the API."""
    pat = os.getenv("PLUMBLINE_AZURE_DEVOPS_PAT", "")
    effective_org = org or os.getenv("PLUMBLINE_AZURE_DEVOPS_ORG", "")
    if not pat or not effective_org:
        console.print("Missing PLUMBLINE_AZURE_DEVOPS_PAT or organisation (--org / PLUMBLINE_AZURE_DEVOPS_ORG).")
        raise typer.Exit(code=2)
    try:
        settings = Settings(azure_devops_pat=SecretStr(pat), azure_devops_org=effective_org)
        config = CollectionConfig(
            org=org, project=project, projects=selected_projects, all_projects=all_projects,
            output_path=str(output) if output else None, include_maturity=include_maturity,
            write_files=write_files, write_db=write_db, db_strict=db_strict,
            run_label=run_label, repo=repo, branch=branch, commit_sha=commit_sha,
        )
    except (ValidationError, ValueError) as exc:
        console.print(f"Invalid collection configuration: {exc}")
        raise typer.Exit(code=2)
    try:
        result = asyncio.run(execute_collection_run(config, settings))
    except Exception as exc:
        console.print(f"Collection failed: {exc}")
        raise typer.Exit(code=1)
    if result.get("status") != "succeeded":
        console.print(f"Collection {result.get('status')}: {result.get('error') or 'no further details'}")
        error = result.get("error") or ""
        raise typer.Exit(code=2 if db_strict and (error.startswith("PLUMBLINE_DATABASE_URL") or error.startswith("Failed to init DB")) else 1)
    if result.get("completeness", {}).get("complete") is False:
        console.print(f"Collection incomplete: {result['id']}; maturity assessment blocked.")
        for failure in result["completeness"].get("failures", []):
            console.print(str(failure))
        raise typer.Exit(code=1)
    if result.get("assessment_status") == "failed":
        console.print("Maturity assessment failed.")
        raise typer.Exit(code=1)
    console.print(f"Collection succeeded: {result['id']}")
    for name, path in result.get("outputs", {}).items():
        console.print(f"{name}: {path}")


from plumbline.provider_cli import register as register_provider_commands

register_provider_commands(app)

from plumbline.auth_cli import register as register_auth_commands
from plumbline.jobs_cli import register as register_jobs_commands

register_auth_commands(app)
register_jobs_commands(app)

from plumbline.connections_cli import register as register_connections_commands

register_connections_commands(app)


if __name__ == "__main__":
    app()
