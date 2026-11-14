"""Connection mapping; schema is maintained by revision 0004."""
from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel


class Connection(SQLModel, table=True):
    __tablename__ = "connections"

    id: str = Field(primary_key=True)
    provider: str
    name: str
    organisation: str | None = None
    config: dict = Field(sa_column=Column(JSON, nullable=False))
    credential_ciphertext: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    capabilities: list = Field(sa_column=Column(JSON, nullable=False))
    test_status: str = "untested"
    test_result: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
