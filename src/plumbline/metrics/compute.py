"""Deterministic calculations over explicit observations; no provider access."""

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from plumbline.providers.contract import EvidenceState, Identity, Window

from .catalogue import CATALOGUE_VERSION, REVIEW_COVERAGE, TEST_PASS_RATE
from .model import (
    Approval, Coverage, Inventory, Issue, MetricResult, Observation, PullRequest,
    ResultState, TestCounts,
)

T = TypeVar("T")
_DISPOSITIONS = {ResultState.EXEMPT, ResultState.NOT_APPLICABLE, ResultState.INACTIVE}
_PRIORITY = (ResultState.COLLECTION_FAILED, ResultState.UNAVAILABLE, ResultState.MISSING)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fresh(observed_at: datetime | None, window: Window) -> bool:
    return _aware(observed_at) and observed_at <= window.as_of  # type: ignore[operator]


def _count_tests(value: TestCounts | None) -> tuple[int, int, Issue | None]:
    if not isinstance(value, TestCounts) or any(type(n) is not int for n in (value.passed, value.eligible)):
        return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "malformed test counts")
    if value.passed < 0 or value.eligible < 0 or value.passed > value.eligible:
        return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "invalid test counts")
    return value.passed, value.eligible, None


def _count_review(value: PullRequest | None, merged_at: datetime) -> tuple[int, int, Issue | None]:
    if not isinstance(value, PullRequest) or not isinstance(value.approvals, tuple):
        return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "malformed review evidence")
    if any(v is not None and not isinstance(v, str) for v in (value.author_id, value.relevant_revision_id)):
        return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "malformed PR identity")
    if value.opened_at is not None and (not _aware(value.opened_at) or value.opened_at > merged_at):
        return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "invalid PR opening time")
    qualified = False
    uncertainty = None
    approvals: tuple[object, ...] = value.approvals
    for approval in approvals:
        if not isinstance(approval, Approval):
            return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "malformed approval")
        if (any(v is not None and not isinstance(v, str) for v in (approval.reviewer_id, approval.revision_id))
                or (approval.approved_at is not None and not _aware(approval.approved_at))
                or (approval.valid_at_merge is not None and type(approval.valid_at_merge) is not bool)):
            return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "malformed approval fields")
        known_time = _aware(approval.approved_at)
        if known_time and value.opened_at is not None and approval.approved_at < value.opened_at:  # type: ignore[operator]
            return 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "approval before PR opening")
        if known_time and approval.approved_at > merged_at:  # type: ignore[operator]
            continue
        if approval.valid_at_merge is False:
            continue
        if (_nonempty(value.author_id) and _nonempty(approval.reviewer_id)
                and approval.reviewer_id == value.author_id):
            continue
        if (_nonempty(value.relevant_revision_id) and _nonempty(approval.revision_id)
                and approval.revision_id != value.relevant_revision_id):
            continue
        if not known_time:
            uncertainty = Issue(None, ResultState.UNAVAILABLE, "approval time unknown")
            continue
        if approval.valid_at_merge is not True:
            uncertainty = Issue(None, ResultState.UNAVAILABLE, "approval validity at merge unknown")
            continue
        if not all(_nonempty(v) for v in (value.author_id, value.relevant_revision_id,
                                          approval.reviewer_id, approval.revision_id)):
            uncertainty = Issue(None, ResultState.UNAVAILABLE, "review identity or revision unknown")
            continue
        if approval.reviewer_id != value.author_id and approval.revision_id == value.relevant_revision_id:
            qualified = True
    if not qualified and uncertainty is not None:
        return 0, 0, uncertainty
    return int(qualified), 1, None


def _calculate(
    metric_id: str,
    scope: Identity,
    window: Window,
    inventory: Inventory[T],
    minimum_sample: int,
    disposition: ResultState | None,
    count: Callable[[T | None, datetime], tuple[int, int, Issue | None]],
    *,
    empty_tests: bool = False,
) -> MetricResult:
    if not isinstance(window, Window):
        raise ValueError("window must be a Window")
    if (not isinstance(inventory, Inventory) or not isinstance(inventory.observations, tuple)
            or (inventory.expected_ids is not None and not isinstance(inventory.expected_ids, tuple))
            or any(not isinstance(item, Observation) for item in inventory.observations)):
        raise ValueError("inventory must contain tuple observations and optional tuple expected IDs")
    if (not isinstance(scope, Identity) or not _nonempty(scope.connection) or not _nonempty(scope.project)
            or not _nonempty(scope.repository) or scope.build is not None):
        raise ValueError("scope must identify a connection, project and repository")
    if type(minimum_sample) is not int or minimum_sample < 1:
        raise ValueError("minimum sample must be a positive integer")
    if disposition is not None and (not isinstance(disposition, ResultState) or disposition not in _DISPOSITIONS):
        raise ValueError("disposition must be exempt, not applicable or inactive")
    if disposition is not None:
        return MetricResult(metric_id, CATALOGUE_VERSION, scope, window, minimum_sample,
                            disposition, None, None, None, Coverage(0, None), ())

    ids = inventory.expected_ids
    keys = [item.item_id for item in inventory.observations]
    if (any(not _nonempty(key) for key in keys) or len(keys) != len(set(keys))
            or (ids is not None and (any(not _nonempty(key) for key in ids) or len(ids) != len(set(ids))))
            or (ids is not None and not set(keys).issubset(ids))):
        raise ValueError("candidate IDs must be unique, non-empty and within the expected inventory")

    issues: list[Issue] = []
    inventory_state: object = inventory.state
    if not isinstance(inventory_state, EvidenceState):
        issues.append(Issue(None, ResultState.COLLECTION_FAILED, "invalid inventory state"))
    elif inventory_state == EvidenceState.FAILED:
        issues.append(Issue(None, ResultState.COLLECTION_FAILED, inventory.detail or "inventory collection failed"))
    elif inventory_state in (EvidenceState.UNAVAILABLE, EvidenceState.PARTIAL):
        issues.append(Issue(None, ResultState.UNAVAILABLE, inventory.detail or "inventory unavailable or partial"))
    elif inventory_state == EvidenceState.OBSERVED_EMPTY and (keys or ids):
        issues.append(Issue(None, ResultState.COLLECTION_FAILED, "empty inventory contains candidates"))
    if not _fresh(inventory.observed_at, window):
        issues.append(Issue(None, ResultState.UNAVAILABLE, "inventory observation time missing or after cutoff"))
    elif inventory.observed_at is not None and inventory.observed_at < window.end:
        issues.append(Issue(None, ResultState.UNAVAILABLE, "inventory observed before window end"))
    if ids is None:
        issues.append(Issue(None, ResultState.UNAVAILABLE, "expected inventory unknown"))

    by_id: dict[str, Observation[T]] = {item.item_id: item for item in inventory.observations}
    numerator = denominator = observed = 0
    for key in ids if ids is not None else keys:
        item = by_id.get(key)
        if item is None:
            issues.append(Issue(key, ResultState.MISSING, "expected observation missing"))
            continue
        item_state: object = item.state
        if not isinstance(item_state, EvidenceState):
            issues.append(Issue(key, ResultState.COLLECTION_FAILED, "invalid evidence state"))
            continue
        if item_state == EvidenceState.FAILED:
            issues.append(Issue(key, ResultState.COLLECTION_FAILED, item.detail or "collection failed"))
            continue
        if item_state in (EvidenceState.UNAVAILABLE, EvidenceState.PARTIAL):
            issues.append(Issue(key, ResultState.UNAVAILABLE, item.detail or "evidence unavailable or partial"))
            continue
        if not _fresh(item.observed_at, window):
            issues.append(Issue(key, ResultState.UNAVAILABLE, "observation time missing or after cutoff"))
            continue
        if not _aware(item.event_at):
            issues.append(Issue(key, ResultState.UNAVAILABLE, "event time unknown"))
            continue
        event_at = item.event_at
        assert event_at is not None
        assert item.observed_at is not None
        if event_at > item.observed_at:
            issues.append(Issue(key, ResultState.UNAVAILABLE, "event after observation time"))
            continue
        if not window.start <= event_at < window.end:
            observed += 1
            continue
        if item_state == EvidenceState.OBSERVED_EMPTY and empty_tests and item.value is None:
            passed, total, issue = 0, 0, None
        elif item_state == EvidenceState.OBSERVED_EMPTY:
            passed, total, issue = 0, 0, Issue(None, ResultState.COLLECTION_FAILED, "invalid empty observation")
        else:
            passed, total, issue = count(item.value, event_at)
        if issue:
            issues.append(Issue(key, issue.state, issue.detail))
        else:
            observed += 1
            numerator += passed
            denominator += total

    coverage = Coverage(observed, None if ids is None else len(ids))
    if issues:
        state = next(state for state in _PRIORITY if any(issue.state == state for issue in issues))
        return MetricResult(metric_id, CATALOGUE_VERSION, scope, window, minimum_sample,
                            state, None, None, None, coverage, tuple(issues))
    state = ResultState.MEASURED if denominator >= minimum_sample else ResultState.INSUFFICIENT_SAMPLE
    value = 100 * numerator / denominator if state == ResultState.MEASURED else None
    return MetricResult(metric_id, CATALOGUE_VERSION, scope, window, minimum_sample,
                        state, numerator, denominator, value, coverage, ())


def calculate_test_pass_rate(
    scope: Identity, window: Window, inventory: Inventory[TestCounts], minimum_sample: int,
    *, disposition: ResultState | None = None,
) -> MetricResult:
    return _calculate(TEST_PASS_RATE.metric_id, scope, window, inventory, minimum_sample,
                      disposition, lambda value, _: _count_tests(value), empty_tests=True)


def calculate_review_coverage(
    scope: Identity, window: Window, inventory: Inventory[PullRequest], minimum_sample: int,
    *, disposition: ResultState | None = None,
) -> MetricResult:
    return _calculate(REVIEW_COVERAGE.metric_id, scope, window, inventory, minimum_sample,
                      disposition, _count_review)
