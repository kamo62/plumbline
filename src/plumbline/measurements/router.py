"""Post-collection diagnostics. The host must install the existing auth guard."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from plumbline.auth.service import event
from plumbline.measurements import store


class TestPassRateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_sample: int = Field(strict=True, gt=0)
    population_policy: Literal["ado-reported-total/v1"]


MeasurementID = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]


def _principal(request: Request, *, admin: bool = False):
    principal = getattr(request.state, "principal", None)
    if not principal:
        raise HTTPException(401, "Authentication required")
    if principal.get("force_change"):
        raise HTTPException(403, "Password change required")
    if principal.get("role") not in {"viewer", "admin"}:
        raise HTTPException(403, "Viewer required")
    if admin and principal.get("role") != "admin":
        raise HTTPException(403, "Administrator required")
    return principal


def _rollback(db):
    try:
        db.rollback()
    except SQLAlchemyError:
        # A failed connection may itself be unable to roll back.
        pass


def create_router(db_dependency):
    router = APIRouter(prefix="/v1", tags=["measurements"])

    @router.post("/runs/{run_id}/measurements/test-pass-rate")
    def create_measurement(run_id: UUID, body: TestPassRateRequest, request: Request,
                           db=Depends(db_dependency)):
        principal = _principal(request, admin=True)
        try:
            record = store.create_measurement(
                db.connection(), str(run_id), minimum_sample=body.minimum_sample,
                population_policy=body.population_policy,
            )
            event(db, "metric_measurement_requested", principal["id"], record["id"])
            db.commit()
        except store.RunNotFound:
            _rollback(db)
            raise HTTPException(404, "Run not found") from None
        except store.MeasurementError:
            _rollback(db)
            raise HTTPException(409, "Measurement unavailable or conflicting") from None
        except SQLAlchemyError:
            _rollback(db)
            raise HTTPException(503, "Database unavailable") from None
        except Exception:
            _rollback(db)
            raise
        return record

    @router.get("/runs/{run_id}/measurements")
    def list_measurements(run_id: UUID, request: Request, db=Depends(db_dependency)):
        _principal(request)
        try:
            return store.list_measurements(db.connection(), str(run_id))
        except store.RunNotFound:
            _rollback(db)
            raise HTTPException(404, "Run not found") from None
        except store.MeasurementError:
            _rollback(db)
            raise HTTPException(409, "Measurement unavailable or conflicting") from None
        except SQLAlchemyError:
            _rollback(db)
            raise HTTPException(503, "Database unavailable") from None

    def get_record(measurement_id, db):
        try:
            record = store.get_measurement(db.connection(), measurement_id)
        except store.MeasurementError:
            _rollback(db)
            raise HTTPException(409, "Measurement unavailable or conflicting") from None
        except SQLAlchemyError:
            _rollback(db)
            raise HTTPException(503, "Database unavailable") from None
        if record is None:
            raise HTTPException(404, "Measurement not found")
        return record

    @router.get("/measurements/{measurement_id}")
    def get_measurement(measurement_id: MeasurementID, request: Request,
                        db=Depends(db_dependency)):
        _principal(request)
        return get_record(measurement_id, db)

    @router.get("/measurements/{measurement_id}/export.json")
    def export_measurement(measurement_id: MeasurementID, request: Request,
                           db=Depends(db_dependency)):
        _principal(request)
        return JSONResponse(get_record(measurement_id, db), headers={
            "Content-Disposition": f'attachment; filename="measurement-{measurement_id}.json"',
        })

    @router.get("/measurements/{measurement_id}/verification")
    def verify_measurement(measurement_id: MeasurementID, request: Request,
                           db=Depends(db_dependency)):
        _principal(request)
        try:
            result = store.verify_measurement(db.connection(), measurement_id)
        except store.MeasurementError:
            _rollback(db)
            raise HTTPException(409, "Measurement unavailable or conflicting") from None
        except SQLAlchemyError:
            _rollback(db)
            raise HTTPException(503, "Database unavailable") from None
        if result is None:
            raise HTTPException(404, "Measurement not found")
        return result

    return router
