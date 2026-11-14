"""
Shared runner for metrics collection.

Used by both CLI and API to ensure identical orchestration logic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, cast
from datetime import datetime, timezone
from uuid import UUID, uuid4
from pathlib import Path
import asyncio
import logging
import os

from plumbline.config.settings import Settings
from plumbline.config.access import checked_path, validate_org, validate_target
from plumbline.api.models import CollectionConfig
from plumbline.backend.api.ado_client import AzureDevOpsClient
from plumbline.backend.services.factory import MetricsServiceFactory
from plumbline.backend.services.raw_data_collector import RawDataCollector
from plumbline.backend.services.raw_maturity_assessment import RawMaturityAssessmentService
from plumbline.persistence.models import init_engine_and_tables
from plumbline.persistence.sinks import CompositeSink, FileSink, PostgresSink
from plumbline import __version__ as TOOL_VERSION
from plumbline.types import RawData

logger = logging.getLogger(__name__)


class RunResult(dict):
    """Result of a metrics collection run"""


def collection_summary(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise single-project and consolidated collection completeness."""
    metadata = raw_data.get("completeness") or {}
    scopes = [metadata] if "complete" in metadata else list(metadata.values())
    failures = [failure for scope in scopes for failure in scope.get("failures", [])]
    return {"complete": all(scope.get("complete", True) for scope in scopes),
            "failures": failures, "scopes": metadata}


async def execute_collection_run(
    config: CollectionConfig,
    settings: Settings,
    run_id: Optional[str] = None,
    persist_result=None,
    attempt_id: Optional[str] = None,
    progress_callback=None,
    connection_provider=None,
) -> RunResult:
    """
    Execute a metrics collection run (normal collection mode).
    Backfill path is deferred to MVP+1.
    """
    # Ensure run_id
    if run_id is None:
        run_id = str(uuid4())

    result: RunResult = RunResult(
        id=run_id,
        queued_at=datetime.now(timezone.utc),
        started_at=None,
        finished_at=None,
        status="queued",
        error=None,
        artifacts_dir=None,
        outputs={},
    )

    # Resolve org
    effective_org = validate_org(config.org or settings.azure_devops_org)
    if config.project:
        validate_target(config.project)
    settings = settings.model_copy(update={"azure_devops_org": effective_org})

    # Every run owns its directory, including when a custom base is selected.
    run_dir = str(UUID(run_id))
    base = checked_path(config.output_path) if config.output_path else Path("exports/metrics")
    output = checked_path(base.parent / run_dir / base.name)
    if attempt_id is not None:
        output = checked_path(output.parent / str(UUID(attempt_id)) / output.name)
    if config.write_files:
        output.parent.mkdir(parents=True, exist_ok=True)

    # Database engine if needed
    engine = None
    if config.write_db and persist_result is None:
        db_url = settings.database_url or os.getenv("PLUMBLINE_DATABASE_URL")
        if not db_url:
            if config.db_strict:
                result.update(
                    status="failed",
                    error="PLUMBLINE_DATABASE_URL must be set for --write-db",
                    finished_at=datetime.now(timezone.utc),
                )
                return result
            else:
                logger.warning("PLUMBLINE_DATABASE_URL not set; DB writes disabled")
                config = config.model_copy(update={"write_db": False})
        else:
            try:
                # Always require Alembic-managed schema; do not auto-create tables
                engine = init_engine_and_tables(db_url, allow_create_all=False)
            except Exception as ex:
                if config.db_strict:
                    result.update(status="failed", error=f"Failed to init DB: {ex}", finished_at=datetime.now(timezone.utc))
                    return result
                else:
                    logger.warning(f"DB init failed: {ex}; continuing without DB")
                    config = config.model_copy(update={"write_db": False})

    # Flip to running
    result["status"] = "running"
    result["started_at"] = datetime.now(timezone.utc)
    # Pre-populate the directory only for file-writing runs.
    result["artifacts_dir"] = str(output.parent) if config.write_files else None

    # Store status in DB if engine available
    if engine and config.write_db:
        try:
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO runs (
                            id, status, executed_at, started_at, org, project, repo, branch, commit_sha,
                            run_label, environment, tool_version, artifacts_dir
                        )
                        VALUES (
                            :id, 'running', :executed_at, :started_at, :org, :project, :repo, :branch, :commit_sha,
                            :run_label, :env, :version, :artifacts_dir
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            status='running',
                            started_at=:started_at,
                            org=EXCLUDED.org, project=EXCLUDED.project,
                            repo=EXCLUDED.repo, branch=EXCLUDED.branch, commit_sha=EXCLUDED.commit_sha,
                            artifacts_dir=EXCLUDED.artifacts_dir
                        """
                    ),
                    {
                        "id": run_id,
                        "executed_at": result.get("queued_at") or result.get("started_at"),
                        "started_at": result["started_at"],
                        "org": effective_org,
                        "project": config.project,
                        "repo": config.repo,
                        "branch": config.branch,
                        "commit_sha": config.commit_sha,
                        "run_label": config.run_label,
                        "env": settings.run_env,
                        "version": TOOL_VERSION,
                        "artifacts_dir": result.get("artifacts_dir"),
                    },
                )
        except Exception as ex:
            logger.warning(f"Failed to insert run record: {ex}")

    ado_client = None
    provider_snapshot = None
    try:
        if connection_provider == "synthetic":
            from plumbline.connections.execution import synthetic_data
            raw_data_result, provider_snapshot = await synthetic_data(config)
        else:
            ado_client = AzureDevOpsClient(settings)
            raw_collector = cast(
                RawDataCollector,
                MetricsServiceFactory.create_service(
                    "raw",
                    ado_client,
                    minimal=False,
                    enable_delta=False,
                    skip_stale_repos=False,
                    force_year_window=True,
                ),
            )
            if progress_callback is not None:
                raw_collector.progress_callback = progress_callback

            # Load exemptions if configured
            exemption_manager = None
            try:
                from plumbline.config.exemptions import ExemptionManager  # type: ignore
                ef = getattr(settings, 'exemptions_file', None)
                if ef:
                    cfg_path = Path(str(ef))
                    if cfg_path.exists():
                        exemption_manager = ExemptionManager(cfg_path)
            except Exception as ex:
                logger.warning(f"Failed to initialize ExemptionManager: {ex}")

            # Collect raw data
            if config.all_projects:
                raw_data_result = await raw_collector.collect_all_projects_raw_data()
            elif config.projects is not None:
                raw_data_result = await raw_collector.collect_all_projects_raw_data(config.projects)
            else:
                raw_data_result = await raw_collector.collect_all_raw_data(config.project)
        raw_data_dict: Dict[str, Any] = dict(raw_data_result)
        completeness = collection_summary(raw_data_dict)
        result["completeness"] = completeness
        result["assessment_status"] = "not_requested"
        results_container: Dict[str, Any] = {"raw_ado_data": raw_data_dict}

        # Maturity assessment
        if config.include_maturity and not completeness["complete"]:
            # Until metric-level uncertainty exists, do not score incomplete evidence.
            result["assessment_status"] = "blocked_incomplete_collection"
        if config.include_maturity and completeness["complete"]:
            result["assessment_status"] = "completed"
            maturity_service = cast(
                RawMaturityAssessmentService,
                MetricsServiceFactory.create_service(
                    "raw_maturity",
                    ado_client,
                    current_month_weight=settings.current_month_weight,
                    historical_weight=settings.historical_weight,
                    exemption_manager=exemption_manager,
                ),
            )
            maturity_data = maturity_service.assess_raw_data(cast(RawData, raw_data_dict))
            results_container["maturity_data"] = maturity_data

            # Derive calculated metrics (best-effort)
            try:
                repos = maturity_data.get("repositories", {}) if isinstance(maturity_data, dict) else {}
                if isinstance(repos, dict) and repos:
                    bsr_vals: List[float] = []
                    cov_vals: List[float] = []
                    for _rid, rdata in repos.items():
                        qa = (rdata or {}).get("quality_assurance") or {}
                        m = qa.get("metrics") or {}
                        bsr = m.get("build_success_rate")
                        cov = m.get("code_coverage")
                        if isinstance(bsr, (int, float)):
                            bsr_vals.append(float(bsr))
                        if isinstance(cov, (int, float)):
                            cov_vals.append(float(cov))
                    def _mean(xs: List[float]) -> Optional[float]:
                        return sum(xs) / len(xs) if xs else None
                    calc_list: List[Dict[str, Any]] = results_container.get("calculated_metrics") or []
                    agg_bsr = _mean(bsr_vals)
                    agg_cov = _mean(cov_vals)
                    if agg_bsr is not None:
                        calc_list.append({"scope": None, "metric_key": "build_success_rate", "value_float": agg_bsr})
                    if agg_cov is not None:
                        calc_list.append({"scope": None, "metric_key": "coverage", "value_float": agg_cov})
                    if calc_list:
                        results_container["calculated_metrics"] = calc_list
            except Exception:
                pass

        # Persistence
        run_context = {
            "run_id": run_id,  # Pass run_id so PostgresSink can update existing run instead of creating duplicate
            "org": effective_org,
            "project": config.project,
            "repo": config.repo,
            "branch": config.branch,
            "commit_sha": config.commit_sha,
            "run_label": config.run_label,
            "environment": settings.run_env,
            "tool_version": TOOL_VERSION,
            "calculation_version": None,
        }

        if config.write_db and engine:
            # Build composite sink
            sinks: List[Any] = []
            if config.write_files:
                sinks.append(FileSink())
            sinks.append(PostgresSink(engine))
            sink = CompositeSink(sinks)
        elif config.write_files:
            sink = FileSink()
        else:
            sink = None

        written_outputs = {}
        if sink or persist_result is not None:
            payload = {
                "raw_ado_data": results_container.get("raw_ado_data"),
                "provider_snapshot": provider_snapshot,
                "file_output_base": str(output),
                "maturity_data": results_container.get("maturity_data"),
                "calculated_metrics": results_container.get("calculated_metrics", []),
            }
            if persist_result is None:
                written_outputs = sink.persist(payload, run_context) or {}

        # Success
        result.update(
            status="succeeded",
            finished_at=datetime.now(timezone.utc),
            artifacts_dir=str(output.parent) if config.write_files else None,
            outputs=written_outputs,
        )

        if persist_result is not None:
            persist_result(payload, run_context, result)

        # Update DB
        if engine and config.write_db:
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE runs
                        SET status='succeeded', finished_at=:finished_at, artifacts_dir=:artifacts_dir
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": result["id"],
                        "finished_at": result["finished_at"],
                        "artifacts_dir": result["artifacts_dir"],
                    },
                )

        return result
    except asyncio.CancelledError:
        if engine and config.write_db:
            try:
                from sqlalchemy import text
                with engine.begin() as conn:
                    conn.execute(text("UPDATE runs SET status='cancelled', finished_at=:finished_at WHERE id=:id"),
                                 {"id": run_id, "finished_at": datetime.now(timezone.utc)})
            except Exception:
                logger.exception("Failed to record cancellation for run %s", run_id)
        raise
    except Exception as e:  # noqa: BLE001
        error = "Connection collection failed" if config.connection_id else str(e)
        logger.error("Run %s failed: %s", run_id, error, exc_info=not bool(config.connection_id))
        result.update(status="failed", error=error, finished_at=datetime.now(timezone.utc))
        if engine and config.write_db:
            try:
                from sqlalchemy import text
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                            UPDATE runs
                            SET status='failed', finished_at=:finished_at, error=:error
                            WHERE id=:id
                            """
                        ),
                        {"id": run_id, "finished_at": result["finished_at"], "error": result["error"]},
                    )
            except Exception:
                pass
        return result
    finally:
        # Close the client even if service construction or collection fails.
        try:
            if ado_client is not None:
                await asyncio.wait_for(ado_client.close(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("ADO client close() timed out after 30s")
        except Exception as e:
            logger.error(f"Error closing ADO client: {e}")
