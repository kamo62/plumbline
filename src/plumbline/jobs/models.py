from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, DateTime, ForeignKey, String, Text
from sqlmodel import Field, SQLModel
from plumbline.persistence.models import UUIDString


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    id: UUID = Field(default_factory=uuid4, sa_column=Column(UUIDString(), primary_key=True))
    run_id: UUID = Field(sa_column=Column(UUIDString(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True))
    config: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(sa_column=Column(String(32), nullable=False, index=True))
    owner: str | None = Field(default=None, sa_column=Column(String(36)))
    lease_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    heartbeat_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    cancel_requested_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    attempts: int = Field(default=0, nullable=False)
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
