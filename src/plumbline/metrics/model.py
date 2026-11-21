"""Inputs and results for the bounded, provider-neutral metric calculations."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from plumbline.providers.contract import EvidenceState, Identity, Window


class ResultState(str, Enum):
    MEASURED = "measured"
    MISSING = "missing"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    UNAVAILABLE = "unavailable"
    COLLECTION_FAILED = "collection_failed"
    EXEMPT = "exempt"
    NOT_APPLICABLE = "not_applicable"
    INACTIVE = "inactive"


T = TypeVar("T")


@dataclass(frozen=True)
class Observation(Generic[T]):
    """One expected candidate, identified within the result's repository scope."""

    item_id: str
    event_at: datetime | None
    state: EvidenceState
    observed_at: datetime | None
    value: T | None
    detail: str | None = None


@dataclass(frozen=True)
class Inventory(Generic[T]):
    """Expected candidate IDs, if known exhaustively, and retrieved observations."""

    state: EvidenceState
    observed_at: datetime | None
    expected_ids: tuple[str, ...] | None
    observations: tuple[Observation[T], ...]
    detail: str | None = None


@dataclass(frozen=True)
class TestCounts:
    """Eligible reported test executions and the subset reported as passed."""

    __test__ = False

    passed: int
    eligible: int


@dataclass(frozen=True)
class Approval:
    reviewer_id: str | None
    revision_id: str | None
    approved_at: datetime | None
    valid_at_merge: bool | None


@dataclass(frozen=True)
class PullRequest:
    author_id: str | None
    relevant_revision_id: str | None
    approvals: tuple[Approval, ...]
    opened_at: datetime | None = None


@dataclass(frozen=True)
class Issue:
    item_id: str | None
    state: ResultState
    detail: str


@dataclass(frozen=True)
class Coverage:
    """Accounted candidate observations, not the metric's event denominator."""

    observed: int
    expected: int | None

    @property
    def percent(self) -> float | None:
        if self.expected is None or self.expected == 0:
            return None
        return 100 * self.observed / self.expected


@dataclass(frozen=True)
class MetricResult:
    metric_id: str
    catalogue_version: str
    scope: Identity
    window: Window
    minimum_sample: int
    state: ResultState
    numerator: int | None
    denominator: int | None
    value: float | None
    coverage: Coverage
    issues: tuple[Issue, ...]
