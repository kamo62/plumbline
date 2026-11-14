"""Packaged, serialised schema migrations for supported databases."""
from sqlalchemy import inspect, text
from alembic.migration import MigrationContext
from alembic.operations import Operations

from .models import init_engine_and_tables
from .baseline import upgrade
from .revision_0002_jobs import upgrade as jobs_upgrade
from .revision_0003_auth import upgrade as auth_upgrade
from .revision_0004_connections import upgrade as connections_upgrade
from .revision_0005_evidence import upgrade as evidence_upgrade

REVISIONS = (
    ("plumbline_0001", upgrade, {"runs", "raw_metrics", "metrics"}),
    ("plumbline_0002", jobs_upgrade, {"jobs"}),
    ("plumbline_0003", auth_upgrade, {"auth_users", "auth_sessions", "auth_tokens", "auth_attempts", "audit_events"}),
    ("plumbline_0004", connections_upgrade, {"connections"}),
    ("plumbline_0005", evidence_upgrade, {"evidence_snapshots", "evidence_checkpoints"}),
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
