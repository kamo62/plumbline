"""Persistence mappings for OIDC identities and short-lived login transactions."""
from datetime import datetime

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


class OIDCIdentity(SQLModel, table=True):
    __tablename__ = "auth_oidc_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: str = Field(primary_key=True)
    issuer: str
    subject: str
    user_id: str = Field(foreign_key="auth_users.id", unique=True)


class OIDCTransaction(SQLModel, table=True):
    __tablename__ = "auth_oidc_transactions"
    state_hash: str = Field(primary_key=True)
    nonce: str
    verifier: str
    expires_at: datetime = Field(sa_column=Column(DateTime(), nullable=False))
