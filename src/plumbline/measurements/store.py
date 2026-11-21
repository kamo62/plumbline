"""Immutable diagnostic documents backed by the original run snapshot.

Fingerprints detect inconsistent content, not deliberate database rewriting.
All writes participate in the transaction supplied by the caller.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import NoResultFound

from plumbline.collection.store import get_run_snapshot_id, load_snapshot
from plumbline.metrics import snapshot as adapter
from plumbline.metrics.catalogue import CATALOGUE_VERSION
from plumbline.persistence.models import Run


class RunNotFound(LookupError):
    """The requested run does not exist."""


class MeasurementError(ValueError):
    """The source or frozen measurement is missing, inconsistent or invalid."""


metadata = sa.MetaData()
measurements = sa.Table(
    "metric_measurements", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("run_id", sa.String(36), nullable=False),
    sa.Column("snapshot_id", sa.String(64), nullable=False),
    sa.Column("parameters_hash", sa.String(64), nullable=False),
    sa.Column("document", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _run_id(conn, run_id):
    try:
        canonical = str(UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError):
        raise RunNotFound("Run not found") from None
    if conn.scalar(sa.select(Run.id).where(Run.id == canonical)) is None:
        raise RunNotFound("Run not found")
    return canonical


def _parameters(document):
    return {key: document[key] for key in adapter.PARAMETER_KEYS}


def _rows():
    # Read JSON as text so malformed stored JSON is treated as corrupt content,
    # rather than escaping from the driver's result processor before validation.
    return sa.select(measurements.c.id, measurements.c.run_id, measurements.c.snapshot_id,
                     measurements.c.parameters_hash, sa.cast(measurements.c.document, sa.Text).label("document"))


def _record(row):
    """Validate the frozen row without applying today's calculation policy."""
    try:
        document = json.loads(row["document"])
        if not isinstance(document, dict):
            raise ValueError
        run_id = str(row["run_id"])
        if str(UUID(run_id)) != run_id:
            raise ValueError
        snapshot_id = row["snapshot_id"]
        fingerprint = document["fingerprint"]
        payload = {key: value for key, value in document.items() if key != "fingerprint"}
        if (document["run_id"] != run_id or document["snapshot_id"] != snapshot_id
                or not isinstance(fingerprint, str) or fingerprint != row["id"]
                or _digest(payload) != fingerprint or _digest(_parameters(document)) != row["parameters_hash"]):
            raise ValueError
        return {"id": row["id"], "run_id": run_id, "snapshot_id": snapshot_id, "document": document}
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        raise MeasurementError("Stored measurement is inconsistent") from None


def _source(conn, run_id, snapshot_id=None):
    try:
        linked_id = get_run_snapshot_id(conn, run_id)
        if linked_id is None or (snapshot_id is not None and snapshot_id != linked_id):
            raise MeasurementError("Run has no matching original snapshot")
        return linked_id, load_snapshot(conn, linked_id)
    except MeasurementError:
        raise
    except (NoResultFound, ValueError):
        raise MeasurementError("Original snapshot is missing or invalid") from None


def _insert(conn):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(measurements)
    if conn.dialect.name == "postgresql":
        return pg_insert(measurements)
    raise ValueError("Only SQLite and PostgreSQL are supported")


def create_measurement(conn, run_id, *, minimum_sample, population_policy):
    """Freeze one explicit measurement. Identical retries return the same record."""
    parameters = adapter.measurement_parameters(minimum_sample=minimum_sample, population_policy=population_policy)
    canonical = _run_id(conn, run_id)
    snapshot_id, snapshot = _source(conn, canonical)
    try:
        document = adapter.build_test_pass_rate_document(
            snapshot, snapshot_id, minimum_sample=minimum_sample, population_policy=population_policy,
        )
        if not isinstance(document, dict) or _parameters(document) != parameters or document["snapshot_id"] != snapshot_id:
            raise ValueError
        document = {**document, "run_id": canonical}
        if "fingerprint" in document:
            raise ValueError
        fingerprint = _digest(document)
        document["fingerprint"] = fingerprint
        parameters_hash = _digest(parameters)
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        raise MeasurementError("Original snapshot cannot be measured") from None
    conn.execute(_insert(conn).values(
        id=fingerprint, run_id=canonical, snapshot_id=snapshot_id,
        parameters_hash=parameters_hash, document=document, created_at=datetime.now(timezone.utc),
    ).on_conflict_do_nothing())
    row = conn.execute(_rows().where(
        measurements.c.run_id == canonical, measurements.c.parameters_hash == parameters_hash,
    )).mappings().one_or_none()
    if row is None:
        raise MeasurementError("Measurement conflicts with an existing record")
    record = _record(row)
    if record["id"] != fingerprint or record["document"] != document:
        raise MeasurementError("Measurement conflicts with an existing record")
    return record


def get_measurement(conn, measurement_id):
    row = conn.execute(_rows().where(measurements.c.id == measurement_id)).mappings().one_or_none()
    return _record(row) if row is not None else None


def list_measurements(conn, run_id):
    canonical = _run_id(conn, run_id)
    rows = conn.execute(_rows().where(measurements.c.run_id == canonical)
                        .order_by(measurements.c.created_at, measurements.c.id)).mappings()
    return [_record(row) for row in rows]


def verify_measurement(conn, measurement_id):
    """Report content, source and supported-code reproducibility independently."""
    row = conn.execute(_rows().where(measurements.c.id == measurement_id)).mappings().one_or_none()
    if row is None:
        return None
    issues = []
    try:
        record = _record(row)
    except MeasurementError:
        record = None
        issues.append("measurement_inconsistent")
    try:
        _, snapshot = _source(conn, str(row["run_id"]), row["snapshot_id"])
        snapshot_intact = True
    except MeasurementError:
        snapshot = None
        snapshot_intact = False
        issues.append("snapshot_inconsistent")
    reproducible = None
    if record is not None:
        document = record["document"]
        supported_versions = {
            "schema_version": adapter.SCHEMA_VERSION,
            "normaliser_version": adapter.NORMALISER_VERSION,
            "calculation_version": adapter.CALCULATION_VERSION,
            "catalogue_version": CATALOGUE_VERSION,
        }
        if any(document.get(key) != value for key, value in supported_versions.items()):
            issues.append("version_unavailable")
        else:
            try:
                parameters = adapter.measurement_parameters(
                    minimum_sample=document["minimum_sample"], population_policy=document["population_policy"],
                )
                supported = _parameters(document) == parameters
            except (KeyError, TypeError, ValueError):
                supported = False
            if not supported:
                issues.append("definition_or_policy_unavailable")
            elif snapshot_intact:
                try:
                    replay = adapter.build_test_pass_rate_document(
                        snapshot, record["snapshot_id"], minimum_sample=document["minimum_sample"],
                        population_policy=document["population_policy"],
                    )
                    replay = {**replay, "run_id": record["run_id"]}
                    reproducible = _digest(replay) == record["id"]
                except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
                    reproducible = False
                if not reproducible:
                    issues.append("reproduction_mismatch")
    status = "verified" if not issues and reproducible is True else (
        "unavailable" if issues and all(issue in {"version_unavailable", "definition_or_policy_unavailable"}
                                       for issue in issues) else "mismatch"
    )
    return {"id": measurement_id, "content_intact": record is not None, "snapshot_intact": snapshot_intact,
            "reproducible": reproducible, "status": status, "issues": issues}
