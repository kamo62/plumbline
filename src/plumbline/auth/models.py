"""SQLModel mappings for the independently migrated auth tables."""
from datetime import datetime
from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    __tablename__ = "auth_users"
    id: str = Field(primary_key=True)
    username: str = Field(unique=True)
    password_hash: str
    role: str
    active: bool
    force_change: bool


class SessionRecord(SQLModel, table=True):
    __tablename__ = "auth_sessions"
    token_hash: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="auth_users.id")
    csrf_hash: str
    expires_at: datetime = Field(sa_column=Column(DateTime(), nullable=False))


class ApiToken(SQLModel, table=True):
    __tablename__ = "auth_tokens"
    id: str = Field(primary_key=True)
    token_hash: str = Field(unique=True)
    user_id: str = Field(foreign_key="auth_users.id")
    role: str
    expires_at: datetime = Field(sa_column=Column(DateTime(), nullable=False))


class Attempt(SQLModel, table=True):
    __tablename__ = "auth_attempts"
    key: str = Field(primary_key=True)
    failures: int
    until: datetime = Field(sa_column=Column(DateTime(), nullable=False))


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"
    id: str = Field(primary_key=True)
    occurred_at: datetime = Field(sa_column=Column(DateTime(), nullable=False))
    actor_id: str | None = None
    action: str
    subject_id: str | None = None
