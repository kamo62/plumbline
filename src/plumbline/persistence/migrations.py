"""Packaged, serialised schema migrations for supported databases."""
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.util import asbool
from alembic.migration import MigrationContext
from alembic.operations import Operations

from .models import init_engine_and_tables
from .baseline import upgrade
from .revision_0002_jobs import upgrade as jobs_upgrade
from .revision_0003_auth import upgrade as auth_upgrade
from .revision_0004_connections import upgrade as connections_upgrade
from .revision_0005_evidence import upgrade as evidence_upgrade
from .revision_0006_oidc import upgrade as oidc_upgrade
from .revision_0007_run_evidence import upgrade as run_evidence_upgrade
from .revision_0008_metric_measurements import upgrade as metric_measurements_upgrade

REVISIONS = (
    ("plumbline_0001", upgrade, {"runs", "raw_metrics", "metrics"}),
    ("plumbline_0002", jobs_upgrade, {"jobs"}),
    ("plumbline_0003", auth_upgrade, {"auth_users", "auth_sessions", "auth_tokens", "auth_attempts", "audit_events"}),
    ("plumbline_0004", connections_upgrade, {"connections"}),
    ("plumbline_0005", evidence_upgrade, {"evidence_snapshots", "evidence_checkpoints"}),
    ("plumbline_0006", oidc_upgrade, {"auth_oidc_identities", "auth_oidc_transactions"}),
    ("plumbline_0007", run_evidence_upgrade, {"run_evidence"}),
    ("plumbline_0008", metric_measurements_upgrade, {"metric_measurements"}),
)
HEAD = REVISIONS[-1][0]
TABLES = set().union(*(tables for _, _, tables in REVISIONS))


class UnsupportedSchemaError(RuntimeError):
    pass


def _state(connection):
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables:
        if tables & TABLES:
            raise UnsupportedSchemaError("Unsupported legacy schema. Back up the database and migrate its data separately to a fresh Plumbline database; no automatic conversion is available.")
        return None
    versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    names = [name for name, _, _ in REVISIONS]
    if len(versions) != 1 or versions[0] not in names:
        raise UnsupportedSchemaError("Unsupported database revision. Back up the database and migrate its data separately to a fresh Plumbline database; no automatic conversion is available.")
    current = names.index(versions[0])
    required = set().union(*(items for _, _, items in REVISIONS[:current + 1]))
    if not required.issubset(tables):
        raise UnsupportedSchemaError("Incomplete Plumbline schema. Restore a backup or migrate to a fresh database.")
    return versions[0]


def status(url):
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database
        if any(not isinstance(value, str) for value in parsed.query.values()):
            raise ValueError("SQLite URI options must have one value")
        uri = asbool(parsed.query.get('uri', False))
        mode = parsed.query.get('mode')
        filename = database
        memory = not database or database == ':memory:'
        if uri:
            if not database or not database.startswith('file:'):
                raise ValueError("Unsupported SQLite file URI for a read-only status check")
            parts = urlsplit(database)
            if not parts.path or parts.netloc not in {"", "localhost"} or parts.query or parts.fragment:
                raise ValueError("Unsupported SQLite file URI for a read-only status check")
            if mode is not None and mode not in {'ro', 'rw', 'rwc', 'memory'}:
                raise ValueError("Unsupported SQLite URI mode")
            memory = parts.path == ':memory:' or mode == 'memory'
            filename = unquote(parts.path)
        if not memory:
            path = Path(filename).resolve()
            try:
                path.stat()
            except FileNotFoundError:
                return None
            # Override even an explicit mode=rwc URI. Checking must never create a file.
            parsed = parsed.set(database="file:" + quote(str(path), safe="/"),
                                query={**parsed.query, "mode": "ro", "uri": "true"})
            url = str(parsed)
    engine = init_engine_and_tables(url)
    with engine.connect() as connection:
        return _state(connection)


def migrate(url):
    engine = init_engine_and_tables(url)
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(786512104)"))
        else:
            raise ValueError("Only SQLite and PostgreSQL are supported")
        try:
            current = _state(connection)
            names = [name for name, _, _ in REVISIONS]
            offset = names.index(current) + 1 if current else 0
            op = Operations(MigrationContext.configure(connection))
            if current is None:
                op.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            for name, apply, _ in REVISIONS[offset:]:
                apply(op)
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES (:version)"), {"version": name})
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return HEAD
