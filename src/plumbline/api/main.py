from __future__ import annotations

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.engine import Engine
from pathlib import Path
from uuid import UUID
import asyncio
import zipfile
import tempfile
import logging
import secrets
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from pydantic import ValidationError

import yaml

from plumbline.config.settings import Settings
from plumbline.auth.guard import install_guard
from plumbline.auth.router import create_router
from plumbline.persistence.models import session_for
from plumbline.api.models import CollectionConfig
from plumbline.jobs import queue
from plumbline.persistence.models import init_engine_and_tables
from plumbline.config.exemptions import ExemptionManager, ExemptionRule, ExemptionConfig
from plumbline.config.access import checked_path
from plumbline import __version__ as TOOL_VERSION
from plumbline.utils.logging import configure_logging

app = FastAPI(title="Plumbline", version=TOOL_VERSION)
logger = logging.getLogger(__name__)

_settings: Settings | None = None


def _db_datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_engine(settings: Settings = Depends(get_settings)) -> Engine:
    db_url = settings.database_url or os.getenv("PLUMBLINE_DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=503, detail="PLUMBLINE_DATABASE_URL not configured")
    return init_engine_and_tables(db_url, allow_create_all=False)


# Static mount for artifacts and UI
EXPORTS_DIR = checked_path("exports").resolve()
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

UI_DIR = Path(__file__).parent / "ui"
DIST_DIR = Path(__file__).resolve().parents[3] / "frontend" / "dist"
ACTIVE_UI_DIR = DIST_DIR if (DIST_DIR / "index.html").is_file() else UI_DIR
SPA_ROUTES = {"", "tokens", "users", "audit", "connections"}


class AppStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        if ACTIVE_UI_DIR == DIST_DIR and path.strip("/") in SPA_ROUTES:
            path = "index.html"
        if path in {"", ".", "index.html"}:
            nonce = scope.get("state", {}).get("csp_nonce", "")
            html = (ACTIVE_UI_DIR / "index.html").read_text()
            html = html.replace("</head>", f'<meta name="csp-nonce" content="{nonce}"></head>')
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        return await super().get_response(path, scope)


def public_ui(path: str) -> bool:
    if not path.startswith("/ui/"):
        return False
    relative = path[4:]
    if ACTIVE_UI_DIR == DIST_DIR and relative.strip("/") in SPA_ROUTES:
        return True
    allowed = (relative == "index.html" or relative.startswith("assets/")) if ACTIVE_UI_DIR == DIST_DIR else relative in {"", "index.html", "app.js", "styles.css"}
    if not allowed:
        return False
    candidate = (ACTIVE_UI_DIR / (relative or "index.html")).resolve()
    return candidate.is_relative_to(ACTIVE_UI_DIR.resolve()) and candidate.is_file()


def request_database_url(request):
    dependency = request.app.dependency_overrides.get(get_settings, get_settings)
    return dependency().database_url


def get_db(settings: Settings = Depends(get_settings)):
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Database not configured")
    with session_for(settings.database_url) as db:
        yield db


_cookie_setting = get_settings()
app.include_router(create_router(get_db, secure_cookie=(
    _cookie_setting.cookie_secure if _cookie_setting.cookie_secure is not None
    else _cookie_setting.run_env not in {"local", "ci"}
)))
from plumbline.connections.router import create_router as create_connections_router

app.include_router(create_connections_router(get_db))
install_guard(app, request_database_url, public_ui=public_ui)
if ACTIVE_UI_DIR.exists():
    app.mount("/ui", AppStaticFiles(directory=str(ACTIVE_UI_DIR), html=True), name="ui")


@app.middleware("http")
async def security_headers(request, call_next):
    nonce = secrets.token_urlsafe(24)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src 'self'; style-src 'self' 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.on_event("startup")
def setup_logging() -> None:
    configure_logging()


@app.on_event("startup")
async def _check_and_maybe_run_migrations():
    """Require a current schema, with optional local automatic migration."""
    from plumbline.persistence.migrations import HEAD, migrate, status
    settings = app.dependency_overrides.get(get_settings, get_settings)()
    url = settings.database_url
    if not url:
        raise RuntimeError("Database not configured. Set PLUMBLINE_DATABASE_URL and run plumbline migrate.")
    auto = (os.getenv("PLUMBLINE_AUTO_RUN_MIGRATIONS") or "false").lower() in {"1", "true", "yes"}
    if auto and settings.run_env in {"local", "dev"}:
        migrate(url)
    elif status(url) != HEAD:
        raise RuntimeError("Database schema is not current. Run plumbline migrate.")


@app.get("/")
async def root():
    if ACTIVE_UI_DIR.exists():
        return RedirectResponse(url="/ui/")
    return {"message": "DevOps Metrics API"}


@app.get("/healthz")
def healthz(settings: Settings = Depends(get_settings)):
    try:
        engine = get_engine(settings)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT status, COUNT(*) FROM runs WHERE status IN ('running', 'queued') GROUP BY status")).all()
        counts = dict(rows)
        return {"status": "ok", "database": "connected", "active_runs": counts.get("running", 0),
                "queued_runs": counts.get("queued", 0), "active_tasks": 0}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "disconnected"})


@app.post("/v1/runs", status_code=202)
def trigger_run(config: CollectionConfig, settings: Settings = Depends(get_settings)):
    from plumbline.connections.execution import ConnectionValidationError
    try:
        run_id = queue.enqueue(get_engine(settings), config, settings)
    except ConnectionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not enqueue run")
        raise HTTPException(status_code=503, detail="Could not persist run") from exc
    return {"id": run_id, "message": "Accepted"}


def _run_record(row):
    import json
    started, finished = _db_datetime(row[2]), _db_datetime(row[3])
    result = row[6]
    if isinstance(result, str):
        result = json.loads(result)
    result = result or {}
    return {"id": row[0], "status": row[1], "started_at": started, "finished_at": finished,
            "duration_seconds": (finished - started).total_seconds() if finished and started else None,
            "error": row[4], "artifacts_dir": row[5], "outputs": result.get("outputs", {}),
            "completeness": result.get("completeness"), "assessment_status": result.get("assessment_status"),
            "repositories": result.get("repositories", {}), "attempt": result.get("attempt", 0),
            "max_attempts": result.get("max_attempts"), "recovery_reason": result.get("recovery_reason"),
            "snapshot_id": result.get("snapshot_id")}


@app.get("/v1/runs/{id}")
def get_run_status(id: UUID, settings: Settings = Depends(get_settings)):
    with get_engine(settings).connect() as conn:
        row = conn.execute(text("SELECT CAST(r.id AS TEXT), r.status, r.started_at, r.finished_at, r.error, r.artifacts_dir, j.result FROM runs r LEFT JOIN jobs j ON j.run_id=r.id WHERE r.id=:id"), {"id": str(id)}).first()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_record(row)


async def _cancel_run_impl(id: UUID, settings: Settings):
    state = await asyncio.to_thread(queue.cancel, get_engine(settings), id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if state not in ("cancelled", "cancelling"):
        raise HTTPException(status_code=400, detail=f"Run already {state}, cannot cancel")
    return {"id": str(id), "status": state, "message": "Cancellation requested"}


@app.delete("/v1/runs/{id}")
async def cancel_run(id: UUID, settings: Settings = Depends(get_settings)):
    """Cancel a running job (DELETE method for backward compatibility)."""
    return await _cancel_run_impl(id, settings)


@app.post("/v1/runs/{id}/cancel")
async def cancel_run_post(id: UUID, settings: Settings = Depends(get_settings)):
    """Cancel a running job (POST method, matches UI bulk-cancel pattern)."""
    return await _cancel_run_impl(id, settings)


@app.delete("/v1/runs/{id}/data")
def delete_run_data(id: UUID, settings: Settings = Depends(get_settings)):
    """Delete a run and all its associated data from the database (metrics, raw_metrics)."""
    run_id_str = str(id)

    try:
        engine = get_engine(settings)
        with engine.begin() as conn:
            # Check if run exists
            result = conn.execute(
                text("SELECT id, run_label, status FROM runs WHERE id = :id"),
                {"id": run_id_str}
            )
            run_row = result.fetchone()
            if not run_row:
                raise HTTPException(status_code=404, detail="Run not found in database")

            active = conn.execute(text("UPDATE jobs SET status=status WHERE run_id=:id AND status NOT IN ('queued', 'running') RETURNING status"), {"id": run_id_str}).scalar()
            existing_job = conn.execute(text("SELECT status FROM jobs WHERE run_id=:id"), {"id": run_id_str}).scalar()
            if existing_job and not active:
                raise HTTPException(status_code=409, detail="Cannot delete an active run")
            snapshots = conn.execute(text("SELECT (SELECT COUNT(*) FROM raw_metrics WHERE run_id=:id) + (SELECT COUNT(*) FROM metrics WHERE run_id=:id)"), {"id": run_id_str}).scalar()
            if snapshots:
                raise HTTPException(status_code=409, detail="Cannot delete a run with stored snapshots")
            if run_row[2] in ("queued", "running"):
                raise HTTPException(status_code=409, detail="Cannot delete an active run")
            # The exact target is the requested UUID, after the active/snapshot checks.
            conn.execute(text("DELETE FROM runs WHERE id=:id"), {"id": run_id_str})
            metrics_deleted = raw_metrics_deleted = 0

        return {
            "id": run_id_str,
            "message": "Run data deleted successfully",
            "deleted": {
                "metrics": metrics_deleted,
                "raw_metrics": raw_metrics_deleted,
                "run": 1
            }
        }
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"Failed to delete run {run_id_str}: {ex}")
        raise HTTPException(status_code=500, detail=f"Failed to delete run: {str(ex)}")


@app.get("/v1/runs")
def list_runs(limit: int = 20, status: str | None = None, settings: Settings = Depends(get_settings)):
    query = "SELECT CAST(r.id AS TEXT), r.status, r.started_at, r.finished_at, r.error, r.artifacts_dir, j.result, r.run_label FROM runs r LEFT JOIN jobs j ON j.run_id=r.id"
    params = {"limit": max(0, limit)}
    if status:
        query += " WHERE r.status=:status"
        params["status"] = status
    query += " ORDER BY COALESCE(r.finished_at, r.started_at, r.executed_at) DESC LIMIT :limit"
    with get_engine(settings).connect() as conn:
        rows = conn.execute(text(query), params).all()
    return [{**_run_record(row), "run_label": row[7]} for row in rows]


@app.get("/v1/runs/{id}/artifacts")
async def list_artifacts(id: UUID, settings: Settings = Depends(get_settings)):
    run_id_str = str(id)
    art_dir_val: Optional[str] = None
    with get_engine(settings).connect() as conn:
        row = conn.execute(text("SELECT artifacts_dir FROM runs WHERE id=:id"), {"id": run_id_str}).fetchone()
    if row and row[0]:
        art_dir_val = row[0]
    if not art_dir_val:
        raise HTTPException(status_code=404, detail="Artifacts not found")

    art_dir = Path(art_dir_val).resolve()

    try:
        if art_dir == EXPORTS_DIR or art_dir.name != run_id_str:
            raise ValueError("Invalid run-owned directory")
        art_dir.relative_to(EXPORTS_DIR)
    except ValueError:
        logger.warning(f"Artifacts directory {art_dir} is outside EXPORTS_DIR {EXPORTS_DIR}")
        raise HTTPException(status_code=403, detail="Invalid artifacts directory")

    if not art_dir.exists():
        raise HTTPException(status_code=404, detail="Artifacts directory missing")

    files = []
    for p in art_dir.glob("**/*"):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            resolved = p.resolve()
            resolved.relative_to(art_dir)
            rel = resolved.relative_to(EXPORTS_DIR)
            run_rel = resolved.relative_to(art_dir)
            files.append({"name": str(rel), "size_bytes": p.stat().st_size, "url": f"/v1/runs/{id}/artifacts/{quote(str(run_rel), safe='/')}"})
        except Exception:  # noqa: BLE001
            continue
    return {"id": str(id), "artifacts_dir": str(art_dir), "files": files}


@app.get("/v1/runs/{id}/artifacts/{file_path:path}")
async def download_artifact(id: UUID, file_path: str, settings: Settings = Depends(get_settings)):
    listing = await list_artifacts(id, settings)
    art_dir = Path(listing["artifacts_dir"])
    relative = Path(file_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=403, detail="Invalid artifact path")
    candidate = art_dir / relative
    if any(part.is_symlink() for part in [candidate, *candidate.parents] if part != art_dir.parent):
        raise HTTPException(status_code=403, detail="Invalid artifact path")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(art_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid artifact path")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(resolved, filename=resolved.name)


@app.get("/v1/runs/{id}/artifacts.zip")
async def download_artifacts_zip(id: UUID, background_tasks: BackgroundTasks, settings: Settings = Depends(get_settings)):
    run_id_str = str(id)
    art_dir_val: Optional[str] = None
    with get_engine(settings).connect() as conn:
        row = conn.execute(text("SELECT artifacts_dir FROM runs WHERE id=:id"), {"id": run_id_str}).fetchone()
    if row and row[0]:
        art_dir_val = row[0]
    if not art_dir_val:
        raise HTTPException(status_code=404, detail="Artifacts not found")

    art_dir = Path(art_dir_val).resolve()

    try:
        if art_dir == EXPORTS_DIR or art_dir.name != run_id_str:
            raise ValueError("Invalid run-owned directory")
        art_dir.relative_to(EXPORTS_DIR)
    except ValueError:
        logger.warning(f"Artifacts directory {art_dir} is outside EXPORTS_DIR {EXPORTS_DIR}")
        raise HTTPException(status_code=403, detail="Invalid artifacts directory")

    if not art_dir.exists():
        raise HTTPException(status_code=404, detail="Artifacts directory missing")

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in art_dir.glob("**/*"):
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                    resolved = p.resolve()
                    resolved.relative_to(art_dir)
                    zf.write(resolved, arcname=resolved.relative_to(art_dir))
                except Exception:  # noqa: BLE001
                    continue

    except Exception as e:  # noqa: BLE001
        zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to create artifacts zip: {e}")

    # Defer cleanup until after response completes
    background_tasks.add_task(lambda: zip_path.unlink(missing_ok=True))
    return FileResponse(str(zip_path), media_type="application/zip", filename=f"{id}_artifacts.zip")


# --------- Projects & Exemptions (Settings) ---------

def _exemptions_path(settings: Settings) -> Path:
    p = settings.exemptions_file or Path("src/plumbline/config/exemptions.yaml")
    return Path(p)


@app.get("/v1/projects/live")
async def list_live_projects(org: str | None = None, settings: Settings = Depends(get_settings)):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    from plumbline.config.access import validate_org

    try:
        effective_org = validate_org(org or settings.azure_devops_org)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        async with AzureDevOpsClient(settings.model_copy(update={"azure_devops_org": effective_org})) as client:
            projects = await client.get_projects()
        return {"items": sorted({p["name"] for p in projects if p.get("name")})}
    except Exception:
        logger.exception("Failed to load projects")
        raise HTTPException(status_code=502, detail="Could not load projects from Azure DevOps")


@app.get("/v1/projects")
async def list_projects(settings: Settings = Depends(get_settings)):
    items: List[str] = []
    db_url = settings.database_url or os.getenv("PLUMBLINE_DATABASE_URL")
    if db_url:
        try:
            engine = init_engine_and_tables(db_url, allow_create_all=False)
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT DISTINCT project FROM runs WHERE project IS NOT NULL ORDER BY project"))
                items = [r[0] for r in rows if r and r[0]]
        except Exception:
            logger.exception("Failed to list projects")
    return {"items": items}


@app.get("/v1/exemptions")
async def get_exemptions(settings: Settings = Depends(get_settings)):
    path = _exemptions_path(settings)
    mgr = ExemptionManager(path)
    cfg = mgr.config
    if not cfg:
        return {"version": "1.0", "exemptions": {}, "pattern_exemptions": []}
    # Convert pydantic models to plain dicts
    ex = {k: v.model_dump() for k, v in (cfg.exemptions or {}).items()}
    pats = [p.model_dump() for p in (cfg.pattern_exemptions or [])]
    return {"version": cfg.version, "exemptions": ex, "pattern_exemptions": pats}


@app.get("/v1/exemptions/meta")
async def get_exemptions_meta():
    metrics = [
        # Quality Assurance
        "code_coverage", "test_pass_rate", "build_success_rate",
        # Deployment Pipeline
        "deployment_frequency", "build_time", "pipeline_stability",
        # Security & Compliance
        "policy_compliance", "security_scanning", "branch_protection",
        # Collaboration Metrics
        "merge_rate", "review_coverage",
    ]
    dimensions = [
        "quality_assurance", "deployment_pipeline", "security_compliance", "collaboration_metrics"
    ]
    return {"metrics": metrics, "dimensions": dimensions}


@app.get("/v1/exemptions/{project}")
async def get_project_exemption(project: str, settings: Settings = Depends(get_settings)):
    path = _exemptions_path(settings)
    mgr = ExemptionManager(path)
    cfg = mgr.config
    if not cfg:
        return {"project": project, "rule": None}
    for name, rule in (cfg.exemptions or {}).items():
        if name.lower() == (project or "").lower():
            return {"project": name, "rule": rule.model_dump()}
    return {"project": project, "rule": None}


@app.put("/v1/exemptions/{project}")
async def put_project_exemption(
    project: str,
    body: Dict[str, Any],
    settings: Settings = Depends(get_settings),
):
    path = _exemptions_path(settings)
    try:
        data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Expected a mapping")
        ex = data.get("exemptions") or {}
        key = next ( (k for k in ex if k.lower() == project.lower()), project )
        existing = ex.get(key) or {}
        unknown = set(body) - set(ExemptionRule.model_fields)
        if unknown:
            raise ValueError(f"Unknown exemption fields: {sorted(unknown)}")
        merged = {**existing, **body}
        merged.setdefault("effective_date", date.today())
        merged.setdefault("level", "metric")
        rule = ExemptionRule.model_validate(merged)
        ex[key] = rule.model_dump(mode="json")
        data.update(version=str(data.get("version") or "1.0"), exemptions=ex)
        data.setdefault("pattern_exemptions", [])
        ExemptionConfig.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False))
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid exemptions data: {exc}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return {"project": key, "rule": ex[key]}


@app.delete("/v1/exemptions/{project}")
async def delete_project_exemption(
    project: str,
    settings: Settings = Depends(get_settings),
):
    """Delete an exemption for a specific project."""
    path = _exemptions_path(settings)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Exemptions file not found")

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read exemptions file: {e}")

    ex: Dict[str, Any] = data.get("exemptions") or {}

    # Find and remove the project (case-insensitive match)
    project_key = None
    for key in ex.keys():
        if key.lower() == project.lower():
            project_key = key
            break

    if project_key is None:
        raise HTTPException(status_code=404, detail=f"Exemption for project '{project}' not found")

    # Remove the exemption
    del ex[project_key]
    data["exemptions"] = ex

    # Write back
    try:
        with open(path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write exemptions file: {e}")

    return {"message": f"Exemption for project '{project_key}' deleted successfully", "project": project_key}
