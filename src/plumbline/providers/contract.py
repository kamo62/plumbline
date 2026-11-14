"""Source-neutral, full-window evidence passed to future assessment code."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class EvidenceState(str, Enum):
    OBSERVED = "observed"
    OBSERVED_EMPTY = "observed_empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class Window:
    """Event window is [start, end); as_of is the requested evidence cutoff."""
    start: datetime
    end: datetime
    as_of: datetime

    def __post_init__(self) -> None:
        if any(x.tzinfo is None or x.utcoffset() is None for x in (self.start, self.end, self.as_of)):
            raise ValueError("window timestamps must be timezone-aware")
        if not self.start < self.end <= self.as_of:
            raise ValueError("expected start < end <= as_of")


@dataclass(frozen=True)
class Identity:
    connection: str
    project: str
    repository: str | None = None
    build: str | None = None


@dataclass(frozen=True)
class Evidence:
    value: Any
    state: EvidenceState
    source: str
    observed_at: datetime | None
    error_code: str | None = None
    detail: str | None = None
    # An observation after as_of cannot establish historical evidence at as_of.
    fresh_as_of: bool | None = None


@dataclass
class Record:
    identity: Identity
    fields: dict[str, Evidence] = field(default_factory=dict)


@dataclass
class Snapshot:
    connection: str
    window: Window
    capabilities: frozenset[str]
    projects: list[Record] = field(default_factory=list)
    repositories: list[Record] = field(default_factory=list)
    builds: list[Record] = field(default_factory=list)


@runtime_checkable
class SCMProvider(Protocol):
    @property
    def capabilities(self) -> frozenset[str]: ...

    async def collect(self, projects: list[str], window: Window) -> Snapshot: ...
