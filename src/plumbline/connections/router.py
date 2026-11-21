"""Connection API. Host must install the existing auth guard."""
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from sqlmodel import select

from plumbline.auth.service import event
from plumbline.providers.registry import UnsupportedProvider, get_provider, normalise_projects, provider_metadata
from .models import Connection
from .service import CredentialError, encrypt, lock, run_doctor


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projects: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("projects")
    @classmethod
    def projects_valid(cls, projects):
        return normalise_projects(projects)


class ProviderRequirements(BaseModel):
    organisation: Literal["required", "not_supported"]
    projects: Literal["required", "optional"]
    credential: Literal["required_for_execution", "not_supported"]


class ProviderMetadata(BaseModel):
    id: str
    label: str
    status: Literal["implemented_live_unverified", "diagnostic_only"] = Field(
        description="diagnostic_only identifies synthetic development evidence, including queued collections; it does not disable runs")
    capabilities: list[str] = Field(description="Implemented evidence families; not a guarantee of complete or live-verified evidence")
    requirements: ProviderRequirements


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    name: str = Field(min_length=1, max_length=128)
    organisation: str | None = None
    config: Config = Field(default_factory=Config)
    credential: SecretStr | None = None


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    organisation: str | None = None
    config: Config | None = None
    credential: SecretStr | None = None


def admin(request):
    principal = getattr(request.state, "principal", None)
    if not principal or principal.get("role") != "admin" or principal.get("force_change"):
        raise HTTPException(403, "Administrator required")
    return principal


async def parse(request, model):
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(422, "Invalid connection input") from None


def validate(provider, name, organisation, config, credential_present):
    if not name.strip():
        raise HTTPException(422, "Invalid connection input")
    spec = supported_provider(provider)
    try:
        organisation, _ = spec.connection_inputs(organisation, config.model_dump(), credential_present)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return organisation


def supported_provider(provider):
    try:
        return get_provider(provider)
    except UnsupportedProvider:
        raise HTTPException(422, "Connection provider is unsupported") from None


def view(item):
    return {"id": item.id, "provider": item.provider, "name": item.name,
            "organisation": item.organisation, "config": item.config,
            "capabilities": item.capabilities, "test_status": item.test_status,
            "test_result": item.test_result}


def create_router(db_dependency):
    router = APIRouter(prefix="/connections", tags=["connections"])

    @router.get("/providers", response_model=list[ProviderMetadata])
    def providers(request: Request):
        if not getattr(request.state, "principal", None):
            raise HTTPException(401, "Authentication required")
        return provider_metadata()

    @router.get("")
    def list_connections(request: Request, db=Depends(db_dependency)):
        if not getattr(request.state, "principal", None):
            raise HTTPException(401, "Authentication required")
        return [view(item) for item in db.exec(select(Connection)).all()]

    @router.post("", status_code=201)
    async def create(request: Request, db=Depends(db_dependency)):
        principal = admin(request)
        body = await parse(request, Create)
        if "credential" in body.model_fields_set and (body.credential is None or not body.credential.get_secret_value().strip()):
            raise HTTPException(422, "Credential must be non-empty when supplied")
        organisation = validate(body.provider, body.name, body.organisation, body.config, body.credential is not None)
        try:
            encrypted = encrypt(body.credential.get_secret_value()) if body.credential else None
        except CredentialError:
            raise HTTPException(503, "Connection encryption unavailable") from None
        lock(db)
        item = Connection(id=str(uuid4()), provider=body.provider, name=body.name, organisation=organisation,
                          config=body.config.model_dump(), credential_ciphertext=encrypted, capabilities=[], test_status="untested")
        db.add(item)
        event(db, "connection_created", principal["id"], item.id)
        db.commit()
        return view(item)

    @router.patch("/{connection_id}")
    async def update(connection_id: str, request: Request, db=Depends(db_dependency)):
        principal = admin(request)
        body = await parse(request, Update)
        if any(getattr(body, field) is None for field in body.model_fields_set):
            raise HTTPException(422, "Supplied fields cannot be null")
        if body.credential is not None and not body.credential.get_secret_value().strip():
            raise HTTPException(422, "Credential must be non-empty when supplied")
        lock(db)
        item = db.get(Connection, connection_id)
        if not item:
            raise HTTPException(404, "Connection not found")
        supported_provider(item.provider)
        name = body.name if body.name is not None else item.name
        organisation = body.organisation if body.organisation is not None else item.organisation
        try:
            config = body.config if body.config is not None else Config.model_validate(item.config)
        except ValidationError:
            raise HTTPException(422, "Invalid connection input") from None
        organisation = validate(item.provider, name, organisation, config,
                                body.credential is not None or item.credential_ciphertext is not None)
        if body.credential is not None:
            try:
                item.credential_ciphertext = encrypt(body.credential.get_secret_value())
            except CredentialError:
                raise HTTPException(503, "Connection encryption unavailable") from None
        item.name, item.organisation, item.config = name, organisation, config.model_dump()
        item.test_status, item.test_result, item.capabilities = "untested", None, []
        db.add(item)
        event(db, "connection_updated", principal["id"], item.id)
        db.commit()
        return view(item)

    @router.post("/{connection_id}/test")
    async def test_connection(connection_id: str, request: Request, db=Depends(db_dependency)):
        principal = admin(request)
        item = db.get(Connection, connection_id)
        if not item:
            raise HTTPException(404, "Connection not found")
        supported_provider(item.provider)
        # Snapshot the inputs, then release the read transaction during network I/O.
        snapshot = (item.provider, item.organisation, item.config.copy(), item.credential_ciphertext)
        db.expunge(item)
        db.rollback()
        try:
            result = await run_doctor(item)
            status = "issues" if any(c[1] in {"failed", "partial", "unavailable"} for c in result["checks"]) else "passed"
        except UnsupportedProvider:
            raise HTTPException(422, "Connection provider is unsupported") from None
        except CredentialError:
            result, status = {"capabilities": [], "checks": [("connection", "failed", "credential_unavailable", 1)]}, "failed"
        except Exception:
            result, status = {"capabilities": [], "checks": [("connection", "failed", "diagnostic_failed", 1)]}, "failed"
        lock(db)
        current = db.get(Connection, connection_id)
        if not current or (current.provider, current.organisation, current.config, current.credential_ciphertext) != snapshot:
            raise HTTPException(409, "Connection changed during test; retry")
        current.capabilities, current.test_status, current.test_result = list(result["capabilities"]), status, result
        db.add(current)
        event(db, "connection_tested", principal["id"], current.id)
        db.commit()
        return view(current)

    return router
