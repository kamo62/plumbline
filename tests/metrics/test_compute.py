from datetime import datetime, timezone

import pytest

from plumbline.metrics import (
    Approval,
    Inventory,
    Observation,
    PullRequest,
    ResultState,
    TestCounts,
    calculate_review_coverage,
    calculate_test_pass_rate,
)
from plumbline.providers.contract import EvidenceState, Identity, Window


def dt(day, month=1, year=2026):
    return datetime(year, month, day, tzinfo=timezone.utc)


WINDOW = Window(dt(1), dt(1, 2), dt(2, 2))
SCOPE = Identity("connection", "project", "repo")


def observation(key, value, *, event=dt(10), state=EvidenceState.OBSERVED, observed=dt(2, 2)):
    return Observation(key, event, state, observed, value)


def inventory(*items, expected=None, state=EvidenceState.OBSERVED, observed=dt(2, 2)):
    return Inventory(
        state=state,
        observed_at=observed,
        expected_ids=tuple(item.item_id for item in items) if expected is None else expected,
        observations=items,
    )


def test_synthetic_counts_and_zero_are_measured():
    result = calculate_test_pass_rate(
        SCOPE, WINDOW, inventory(observation("run-a", TestCounts(8, 10)),
                                 observation("run-b", TestCounts(10, 10))), 1
    )
    assert (result.state, result.numerator, result.denominator, result.value) == (
        ResultState.MEASURED, 18, 20, 90.0
    )
    assert (result.coverage.observed, result.coverage.expected, result.coverage.percent) == (2, 2, 100)
    zero = calculate_test_pass_rate(SCOPE, WINDOW, inventory(observation("run", TestCounts(0, 10))), 1)
    assert (zero.state, zero.numerator, zero.denominator, zero.value) == (ResultState.MEASURED, 0, 10, 0)


def test_empty_missing_malformed_failed_and_minimum_sample():
    empty = calculate_test_pass_rate(SCOPE, WINDOW, inventory(state=EvidenceState.OBSERVED_EMPTY), 1)
    assert (empty.state, empty.numerator, empty.denominator, empty.value) == (
        ResultState.INSUFFICIENT_SAMPLE, 0, 0, None
    )
    missing = calculate_test_pass_rate(SCOPE, WINDOW, inventory(expected=("missing",)), 1)
    assert missing.state == ResultState.MISSING
    assert (missing.coverage.observed, missing.coverage.expected, missing.coverage.percent) == (0, 1, 0)
    malformed = calculate_test_pass_rate(SCOPE, WINDOW, inventory(observation("r", TestCounts(11, 10))), 1)
    assert malformed.state == ResultState.COLLECTION_FAILED
    failed = calculate_test_pass_rate(
        SCOPE, WINDOW, inventory(observation("r", None, state=EvidenceState.FAILED)), 1
    )
    assert failed.state == ResultState.COLLECTION_FAILED
    assert failed.value is None
    short = calculate_test_pass_rate(SCOPE, WINDOW, inventory(observation("r", TestCounts(8, 10))), 11)
    assert (short.state, short.numerator, short.denominator, short.value) == (
        ResultState.INSUFFICIENT_SAMPLE, 8, 10, None
    )


def test_inventory_failure_and_unknown_coverage_are_not_hidden():
    ok = observation("ok", TestCounts(8, 10))
    failed = observation("failed", None, state=EvidenceState.FAILED)
    result = calculate_test_pass_rate(SCOPE, WINDOW, inventory(ok, failed, expected=("ok", "failed", "missing")), 1)
    assert result.state == ResultState.COLLECTION_FAILED
    assert (result.coverage.observed, result.coverage.expected) == (1, 3)
    assert {issue.item_id for issue in result.issues} == {"failed", "missing"}
    unknown = Inventory(EvidenceState.FAILED, dt(2, 2), None, (ok,))
    result = calculate_test_pass_rate(SCOPE, WINDOW, unknown, 1)
    assert result.state == ResultState.COLLECTION_FAILED
    assert result.coverage.expected is None and result.coverage.percent is None
    assert result.value is None
    independently_known = Inventory(EvidenceState.FAILED, dt(2, 2), ("ok", "missing"), (ok,))
    result = calculate_test_pass_rate(SCOPE, WINDOW, independently_known, 1)
    assert result.state == ResultState.COLLECTION_FAILED
    assert (result.coverage.observed, result.coverage.expected, result.coverage.percent) == (1, 2, 50)
    assert {issue.item_id for issue in result.issues} == {None, "missing"}


def test_old_pr_merged_in_window_and_self_approval_excluded():
    old_pr = PullRequest("author", "revision-2", (Approval("reviewer", "revision-2", dt(9), True),),
                         opened_at=dt(15, 12, 2025))
    self_review = PullRequest("author", "revision-1", (Approval("author", "revision-1", dt(9), True),))
    end = PullRequest("author", "revision-1", (Approval("reviewer", "revision-1", dt(30), True),))
    result = calculate_review_coverage(SCOPE, WINDOW, inventory(
        observation("old-opened-pr", old_pr), observation("self", self_review),
        observation("at-exclusive-end", end, event=dt(1, 2)),
    ), 1)
    assert (result.state, result.numerator, result.denominator, result.value) == (ResultState.MEASURED, 1, 2, 50)
    short = calculate_review_coverage(SCOPE, WINDOW, inventory(observation("old-opened-pr", old_pr)), 2)
    assert (short.state, short.numerator, short.denominator, short.value) == (
        ResultState.INSUFFICIENT_SAMPLE, 1, 1, None
    )


@pytest.mark.parametrize("approval", [
    Approval("reviewer", None, dt(9), True),
    Approval(None, "revision-1", dt(9), True),
    Approval("reviewer", "revision-1", dt(9), None),
])
def test_uncertain_review_is_unavailable_not_an_invented_approval(approval):
    pr = PullRequest("author", "revision-1", (approval,))
    result = calculate_review_coverage(SCOPE, WINDOW, inventory(observation("pr", pr)), 1)
    assert result.state == ResultState.UNAVAILABLE
    assert result.value is None and result.coverage.observed == 0


def test_freshness_and_explicit_dispositions():
    late = inventory(observation("r", TestCounts(1, 1), observed=dt(3, 2)))
    assert calculate_test_pass_rate(SCOPE, WINDOW, late, 1).state == ResultState.UNAVAILABLE
    for state in (ResultState.EXEMPT, ResultState.NOT_APPLICABLE, ResultState.INACTIVE):
        result = calculate_test_pass_rate(SCOPE, WINDOW, late, 1, disposition=state)
        assert result.state == state and result.value is None
    with pytest.raises(ValueError):
        calculate_test_pass_rate(SCOPE, WINDOW, late, 0)


def test_scoped_ids_and_inventory_observation_time():
    item = observation("same-id", TestCounts(18, 20))
    first = calculate_test_pass_rate(SCOPE, WINDOW, inventory(item), 1)
    other_scope = Identity("connection", "other-project", "repo")
    second = calculate_test_pass_rate(other_scope, WINDOW, inventory(item), 1)
    assert first.value == second.value == 90
    assert first.scope != second.scope
    early_inventory = inventory(item, observed=dt(20))
    assert calculate_test_pass_rate(SCOPE, WINDOW, early_inventory, 1).state == ResultState.UNAVAILABLE
    early_item = inventory(observation("run", TestCounts(1, 1), event=dt(20), observed=dt(19)))
    assert calculate_test_pass_rate(SCOPE, WINDOW, early_item, 1).state == ResultState.UNAVAILABLE


def test_observed_empty_report_partial_inventory_and_revision_semantics():
    empty_report = observation("empty", None, state=EvidenceState.OBSERVED_EMPTY)
    result = calculate_test_pass_rate(SCOPE, WINDOW, inventory(empty_report), 1)
    assert (result.state, result.denominator, result.coverage.percent) == (ResultState.INSUFFICIENT_SAMPLE, 0, 100)
    partial = Inventory(EvidenceState.PARTIAL, dt(2, 2), None, (empty_report,))
    result = calculate_test_pass_rate(SCOPE, WINDOW, partial, 1)
    assert result.state == ResultState.UNAVAILABLE and result.coverage.percent is None
    pr = PullRequest("author", "new", (
        Approval("reviewer", "old", dt(8), True),
        Approval("reviewer", "new", dt(9), False),
        Approval("reviewer", "new", dt(11), True),  # after merge
    ))
    result = calculate_review_coverage(SCOPE, WINDOW, inventory(observation("pr", pr)), 1)
    assert (result.state, result.numerator, result.denominator, result.value) == (ResultState.MEASURED, 0, 1, 0)


def test_duplicate_or_unexpected_candidate_ids_are_rejected():
    item = observation("id", TestCounts(1, 1))
    with pytest.raises(ValueError):
        calculate_test_pass_rate(SCOPE, WINDOW, inventory(item, item), 1)
    with pytest.raises(ValueError):
        calculate_test_pass_rate(SCOPE, WINDOW, inventory(item, expected=("other",)), 1)


def test_approval_before_pr_opening_is_malformed():
    pr = PullRequest("author", "rev", (Approval("reviewer", "rev", dt(2), True),), opened_at=dt(3))
    result = calculate_review_coverage(SCOPE, WINDOW, inventory(observation("pr", pr)), 1)
    assert result.state == ResultState.COLLECTION_FAILED
    assert result.issues[0].detail == "approval before PR opening"


@pytest.mark.parametrize("approvals,expected", [
    ((Approval("author", "rev", None, None),), 0),
    ((Approval("reviewer", "other-rev", None, None),), 0),
    ((Approval(None, None, None, False),), 0),
    ((Approval("reviewer", "rev", dt(9), True), Approval(None, None, None, None)), 100),
    ((Approval(None, None, None, None), Approval("reviewer", "rev", dt(9), True)), 100),
])
def test_irrelevant_approval_uncertainty_does_not_hide_known_result(approvals, expected):
    pr = PullRequest("author", "rev", approvals)
    result = calculate_review_coverage(SCOPE, WINDOW, inventory(observation("pr", pr)), 1)
    assert result.state == ResultState.MEASURED
    assert result.value == expected
    assert result.coverage.percent == 100


def test_qualifying_approval_does_not_hide_malformed_evidence():
    pr = PullRequest("author", "rev", (
        Approval("reviewer", "rev", dt(9), True),
        Approval("author", "rev", dt(2), None),
    ), opened_at=dt(3))
    result = calculate_review_coverage(SCOPE, WINDOW, inventory(observation("pr", pr)), 1)
    assert result.state == ResultState.COLLECTION_FAILED


def test_empty_inventory_cannot_claim_expected_candidates():
    result = calculate_test_pass_rate(
        SCOPE, WINDOW, inventory(expected=("contradiction",), state=EvidenceState.OBSERVED_EMPTY), 1
    )
    assert result.state == ResultState.COLLECTION_FAILED


@pytest.mark.parametrize("bad", [
    Approval("reviewer", "rev", "2026-01-09", True),
    Approval("reviewer", "rev", datetime(2026, 1, 9), True),
    Approval("reviewer", "rev", dt(9), "yes"),
    Approval("reviewer", "rev", dt(9), 1),
    Approval(123, "rev", dt(9), True),
])
@pytest.mark.parametrize("with_good", [False, True])
def test_malformed_approval_fields_never_become_measurements(bad, with_good):
    approvals = (bad, Approval("other", "rev", dt(9), True)) if with_good else (bad,)
    result = calculate_review_coverage(
        SCOPE, WINDOW, inventory(observation("pr", PullRequest("author", "rev", approvals))), 1
    )
    assert result.state == ResultState.COLLECTION_FAILED


def test_metric_container_contracts_are_checked_even_for_dispositions():
    with pytest.raises(ValueError, match="window"):
        calculate_test_pass_rate(SCOPE, None, inventory(), 1, disposition=ResultState.INACTIVE)
    with pytest.raises(ValueError, match="inventory"):
        calculate_test_pass_rate(SCOPE, WINDOW, Inventory(EvidenceState.OBSERVED, dt(2, 2), "a", ()), 1)
