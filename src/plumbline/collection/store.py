"""Immutable full-window snapshots and conservative scoped checkpoints.

Call persist_snapshot with the transaction connection supplied to jobs.finish's
persist callback. This puts evidence and checkpoints behind the job lease fence.
The provider contract currently cannot request safe incremental enrichment; the
collection entry point therefore always fetches the full requested window.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from plumbline.providers.contract import Evidence, EvidenceState, Identity, Record, Snapshot, Window

FORMAT_VERSION = 1
metadata = sa.MetaData()
snapshots = sa.Table(
    "evidence_snapshots", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("connection_id", sa.String(256), nullable=False),
    sa.Column("window_start", sa.String(32), nullable=False),
    sa.Column("window_end", sa.String(32), nullable=False),
    sa.Column("as_of", sa.String(32), nullable=False),
    sa.Column("format_version", sa.Integer, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
)
checkpoints = sa.Table(
    "evidence_checkpoints", metadata,
    *(sa.Column(name, sa.String(256), primary_key=True) for name in
      ("connection_id", "project_id", "repository_id", "entity_type", "entity_id", "field")),
    sa.Column("window_end", sa.String(32), nullable=False),
    sa.Column("snapshot_id", sa.String(64), nullable=False),
)
run_evidence = sa.Table(
    "run_evidence", metadata,
    sa.Column("run_id", sa.String(36), primary_key=True),
    sa.Column("snapshot_id", sa.String(64), nullable=False),
)


def _time(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _record(record, connection, kind):
    identity = record.identity
    if identity.connection != connection or not identity.project:
        raise ValueError("record must have a matching connection and project")
    if kind == "repositories" and not identity.repository:
        raise ValueError("repository identity required")
    if kind == "builds" and not identity.build:
        raise ValueError("build identity required")
    if kind == "projects" and (identity.repository is not None or identity.build is not None):
        raise ValueError("invalid project identity")
    if kind == "repositories" and identity.build is not None:
        raise ValueError("invalid repository identity")
    return {
        "identity": {"connection": identity.connection, "project": identity.project,
                     "repository": identity.repository, "build": identity.build},
        "fields": {name: {"value": evidence.value, "state": evidence.state.value,
                          "source": evidence.source,
                          "observed_at": _time(evidence.observed_at) if evidence.observed_at else None,
                          "error_code": evidence.error_code, "detail": evidence.detail,
                          "fresh_as_of": evidence.fresh_as_of}
                   for name, evidence in record.fields.items()},
    }


def _payload(snapshot):
    if not snapshot.connection:
        raise ValueError("connection identity required")
    data = {"format_version": FORMAT_VERSION, "connection": snapshot.connection,
            "window": {"start": _time(snapshot.window.start), "end": _time(snapshot.window.end),
                       "as_of": _time(snapshot.window.as_of)},
            "capabilities": sorted(snapshot.capabilities),
            "collection_started_at": _time(snapshot.collection_started_at) if snapshot.collection_started_at else None,
            "collection_completed_at": _time(snapshot.collection_completed_at) if snapshot.collection_completed_at else None}
    for kind in ("projects", "repositories", "builds"):
        data[kind] = [_record(record, snapshot.connection, kind) for record in getattr(snapshot, kind)]
        keys = [(r["identity"]["project"], r["identity"]["repository"], r["identity"]["build"]) for r in data[kind]]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate record identity")
    return data


def _insert(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    if conn.dialect.name == "postgresql":
        return pg_insert(table)
    raise ValueError("Only SQLite and PostgreSQL are supported")


def _digest(data):
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return sha256(encoded.encode()).hexdigest()


def _verified_payload(conn, snapshot_id):
    try:
        data = conn.execute(sa.select(snapshots.c.payload).where(snapshots.c.id == snapshot_id)).scalar_one()
        digest = _digest(data)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("Malformed evidence snapshot payload") from None
    if not isinstance(data, dict) or digest != snapshot_id:
        raise ValueError("Evidence snapshot content does not match its ID")
    if data.get("format_version") != FORMAT_VERSION:
        raise ValueError("Unsupported evidence snapshot version")
    return data


def get_run_snapshot_id(conn, run_id):
    """Return the immutable snapshot ID linked to a run, or None for an unlinked run."""
    canonical_id = str(UUID(str(run_id)))
    return conn.execute(sa.select(run_evidence.c.snapshot_id).where(
        run_evidence.c.run_id == canonical_id)).scalar_one_or_none()


def link_run_snapshot(conn, run_id, snapshot_id):
    """Link once, allowing identical retries but never replacing existing evidence."""
    canonical_id = str(UUID(str(run_id)))
    conn.execute(_insert(conn, run_evidence).values(run_id=canonical_id, snapshot_id=snapshot_id)
                 .on_conflict_do_nothing(index_elements=[run_evidence.c.run_id]))
    if get_run_snapshot_id(conn, canonical_id) != snapshot_id:
        raise ValueError("Run is already linked to a different evidence snapshot")


def persist_snapshot(conn, snapshot):
    """Persist exact inputs atomically; caller owns transaction and job fencing.

    A checkpoint records only a successfully observed individual field. It is
    not a claim that other fields, records or the enclosing project succeeded.
    """
    data = _payload(snapshot)
    digest = _digest(data)
    window = data["window"]
    conn.execute(_insert(conn, snapshots).values(id=digest, connection_id=snapshot.connection,
        window_start=window["start"], window_end=window["end"], as_of=window["as_of"],
        format_version=FORMAT_VERSION, payload=data).on_conflict_do_nothing(index_elements=[snapshots.c.id]))
    _verified_payload(conn, digest)
    for kind in ("projects", "repositories", "builds"):
        for record in data[kind]:
            identity = record["identity"]
            for name, field in record["fields"].items():
                if field["state"] not in (EvidenceState.OBSERVED.value, EvidenceState.OBSERVED_EMPTY.value):
                    continue
                if (field["fresh_as_of"] is False or field["observed_at"] is None
                        or field["observed_at"] > window["as_of"]):
                    continue
                row = dict(connection_id=snapshot.connection, project_id=identity["project"],
                           repository_id=identity["repository"] or "", entity_type=kind,
                           entity_id=identity["build"] or "", field=name,
                           window_end=window["end"], snapshot_id=digest)
                stmt = _insert(conn, checkpoints).values(**row)
                conn.execute(stmt.on_conflict_do_update(
                    index_elements=[checkpoints.c[x] for x in ("connection_id", "project_id", "repository_id", "entity_type", "entity_id", "field")],
                    set_={"window_end": stmt.excluded.window_end, "snapshot_id": stmt.excluded.snapshot_id},
                    where=stmt.excluded.window_end > checkpoints.c.window_end))
    return digest


def load_snapshot(conn, snapshot_id):
    """Read the exact persisted observation, including its original as-of and field states."""
    data = _verified_payload(conn, snapshot_id)
    try:
        window = Window(*(datetime.fromisoformat(data["window"][key]) for key in ("start", "end", "as_of")))
        result = Snapshot(data["connection"], window, frozenset(data["capabilities"]))
        for name in ("collection_started_at", "collection_completed_at"):
            if data.get(name):
                setattr(result, name, datetime.fromisoformat(data[name]))
        for kind in ("projects", "repositories", "builds"):
            for item in data[kind]:
                fields = {}
                for name, ev in item["fields"].items():
                    fields[name] = Evidence(ev["value"], EvidenceState(ev["state"]), ev["source"],
                        datetime.fromisoformat(ev["observed_at"]) if ev["observed_at"] else None,
                        ev["error_code"], ev["detail"], ev["fresh_as_of"])
                getattr(result, kind).append(Record(Identity(**item["identity"]), fields))
        reconstructed = _payload(result)
        # Early format-1 snapshots predate collection timing fields.
        for name in ("collection_started_at", "collection_completed_at"):
            if name not in data:
                reconstructed.pop(name)
        if reconstructed != data:
            raise ValueError
        return result
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Malformed evidence snapshot payload") from None


async def collect_full_window(provider, projects, window):
    """Fetch a reconstructable assessment window; never return a delta as input.

    This is also the incremental fallback. It rechecks open PRs and refreshes
    enrichment for every selected build, including failed and empty evidence.
    Historical source availability remains represented by provider field states.
    """
    result = await provider.collect(projects, window)
    if result.window != window:
        raise ValueError("provider returned a different assessment window")
    return result
