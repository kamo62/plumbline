from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel, create_engine
from sqlalchemy import Column, Text, String, DateTime, ForeignKey, UniqueConstraint, event
from sqlalchemy.types import TypeDecorator
from sqlalchemy.types import JSON


class UUIDString(TypeDecorator):
    """Canonical UUID text shared by ORM and existing SQL consumers."""
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return str(UUID(str(value))) if value is not None else None

    def process_result_value(self, value, dialect):
        return UUID(str(value)) if value is not None else None


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: UUID = Field(default_factory=uuid4, sa_column=Column(UUIDString(), primary_key=True))
    org: Optional[str] = Field(default=None, index=True, max_length=256)
    project: Optional[str] = Field(default=None, index=True, max_length=256)
    repo: Optional[str] = Field(default=None, index=True, max_length=256)
    branch: Optional[str] = Field(default=None, index=True, max_length=256)
    commit_sha: Optional[str] = Field(default=None, index=True, max_length=64)

    executed_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    environment: Optional[str] = Field(default=None, max_length=64)
    run_label: Optional[str] = Field(default=None, max_length=256)

    tool_version: Optional[str] = Field(default=None, max_length=64)
    thresholds_version: Optional[str] = Field(default=None, max_length=64)
    config_hash: Optional[str] = Field(default=None, max_length=128)
    calculation_version: Optional[str] = Field(default=None, max_length=64)

    # Status tracking
    status: Optional[str] = Field(default=None, index=True, max_length=32)
    error: Optional[str] = Field(default=None, sa_column=Column(Text))

    # Timing
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))

    # Artifacts path
    artifacts_dir: Optional[str] = Field(default=None, sa_column=Column(Text))


class RawMetric(SQLModel, table=True):
    __tablename__ = "raw_metrics"
    __table_args__ = (UniqueConstraint("run_id", "source_system", "source_key", name="uq_raw_identity"),)

    id: UUID = Field(default_factory=uuid4, sa_column=Column(UUIDString(), primary_key=True))

    org: Optional[str] = Field(default=None, index=True, max_length=256)
    project: Optional[str] = Field(default=None, index=True, max_length=256)
    repo: Optional[str] = Field(default=None, index=True, max_length=256)

    run_id: UUID = Field(sa_column=Column(UUIDString(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True))
    source_system: str = Field(index=True, max_length=64)
    source_key: str = Field(index=True, max_length=256)
    raw_data: Dict[str, Any] = Field(sa_column=Column(JSON))
    collected_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))


class Metric(SQLModel, table=True):
    __tablename__ = "metrics"
    __table_args__ = (UniqueConstraint("run_id", "scope", "metric_key", name="uq_metric_identity"),)

    id: UUID = Field(default_factory=uuid4, sa_column=Column(UUIDString(), primary_key=True))

    org: Optional[str] = Field(default=None, index=True, max_length=256)
    project: Optional[str] = Field(default=None, index=True, max_length=256)
    repo: Optional[str] = Field(default=None, index=True, max_length=256)

    run_id: UUID = Field(sa_column=Column(UUIDString(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True))
    scope: str = Field(default="", index=True, max_length=256)
    metric_key: str = Field(index=True, max_length=256)

    metric_value: Optional[Decimal] = None
    metric_json: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    score: Optional[Decimal] = None
    status: Optional[str] = Field(default=None, max_length=64)
    calculation_version: Optional[str] = Field(default=None, max_length=64)

    observed_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))


_ENGINE_CACHE: Dict[str, Any] = {}
_ENGINE_CACHE_LOCK = threading.Lock()


def init_engine_and_tables(database_url: str, allow_create_all: bool = False):
    """Return the shared engine. Schema changes require packaged migrations."""
    from sqlalchemy.engine import make_url
    if allow_create_all:
        raise ValueError("Use plumbline db-migrate to initialise the database")
    if make_url(database_url).get_backend_name() not in {"sqlite", "postgresql"}:
        raise ValueError("Only SQLite and PostgreSQL are supported")
    if make_url(database_url).drivername == "postgresql":
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    with _ENGINE_CACHE_LOCK:
        if database_url not in _ENGINE_CACHE:
            engine = create_engine(database_url, pool_pre_ping=True, future=True)
            if engine.dialect.name == "sqlite":
                @event.listens_for(engine, "connect")
                def _sqlite_foreign_keys(dbapi_connection, connection_record):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.close()
            _ENGINE_CACHE[database_url] = engine
        return _ENGINE_CACHE[database_url]


def session_for(database_url: str):
    from sqlmodel import Session
    return Session(init_engine_and_tables(database_url))
