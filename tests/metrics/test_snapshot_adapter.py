import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from plumbline.metrics.catalogue import TEST_PASS_RATE
from plumbline.metrics.snapshot import (
    PARAMETER_KEYS,
    POPULATION_POLICY,
    build_test_pass_rate_document,
    measurement_parameters,
)
from plumbline.providers.contract import Evidence, EvidenceState, Identity, Record, Snapshot, Window


def dt(day, month=1):
    return datetime(2026, month, day, tzinfo=timezone.utc)


WINDOW = Window(dt(1), dt(1, 2), dt(2, 2))


def evidence(value, state=EvidenceState.OBSERVED, *, at=dt(2, 2), fresh=True):
    return Evidence(value, state, "mock", at, "never-expose-this", "secret-error-text", fresh)


def make_snapshot(project="p1", repo="r1", build=7, passed=18, total=20):
    raw_repo = {"id": repo, "name": "repo"}
    raw_build = {"id": build, "repository": {"id": repo}, "status": "completed",
                 "result": "succeeded", "finishTime": "2026-01-10T00:00:00Z"}
    return Snapshot(
        "connection", WINDOW, frozenset({"tests"}),
        projects=[Record(Identity("connection", project), {
            "project": evidence({"id": project, "name": "project"}),
            "repositories": evidence([raw_repo]), "builds": evidence([raw_build]),
        })],
        repositories=[Record(Identity("connection", project, repo), {"repository": evidence(raw_repo)})],
        builds=[Record(Identity("connection", project, build=str(build)), {
            "build": evidence(raw_build),
            "tests": evidence({"results": [{"passed": passed, "total": total}],
                               "unlinked_runs": 0, "linked_runs": 1}),
        })],
    )


def document(snapshot, minimum=1):
    return build_test_pass_rate_document(snapshot, "snapshot-id", minimum_sample=minimum,
                                         population_policy=POPULATION_POLICY)


def result(snapshot, minimum=1):
    return document(snapshot, minimum)["results"][0]


def test_policy_is_explicit_frozen_and_json_safe():
    with pytest.raises(TypeError):
        measurement_parameters()
    with pytest.raises(TypeError):
        build_test_pass_rate_document(make_snapshot(), "id")
    for bad in (0, -1, True, 1.0, "1"):
        with pytest.raises(ValueError):
            measurement_parameters(minimum_sample=bad, population_policy=POPULATION_POLICY)
    with pytest.raises(ValueError):
        measurement_parameters(minimum_sample=1, population_policy="implicit")
    params = measurement_parameters(minimum_sample=1, population_policy=POPULATION_POLICY)
    assert tuple(params) == PARAMETER_KEYS
    assert params["definition"]["metric_id"] == TEST_PASS_RATE.metric_id
    params["definition"]["metric_id"] = "changed"
    assert measurement_parameters(minimum_sample=1, population_policy=POPULATION_POLICY)["definition"]["metric_id"] == "test_pass_rate"
    assert json.loads(json.dumps(document(make_snapshot()), allow_nan=False))["snapshot_id"] == "snapshot-id"


@pytest.mark.parametrize("passed,total,value", [(18, 20, 90), (0, 20, 0)])
def test_measured_counts_and_candidate_coverage(passed, total, value):
    doc = document(make_snapshot(passed=passed, total=total))
    row = doc["results"][0]
    assert doc["inventory_complete"] is True and doc["issues"] == []
    assert (row["state"], row["numerator"], row["denominator"], row["value"]) == ("measured", passed, total, value)
    assert row["scope"] == {"connection": "connection", "project": "p1", "repository": "r1", "build": None}
    assert row["coverage"] == {"observed": 1, "expected": 1, "percent": 100}
    assert row["window"] == doc["window"]


def test_empty_and_reported_zero_are_known_zero_not_zero_percent():
    snapshot = make_snapshot(passed=0, total=0)
    row = result(snapshot)
    assert (row["state"], row["numerator"], row["denominator"], row["value"]) == ("insufficient_sample", 0, 0, None)
    snapshot.builds[0].fields["tests"] = evidence({"results": [], "unlinked_runs": 0}, EvidenceState.OBSERVED_EMPTY)
    row = result(snapshot)
    assert (row["state"], row["denominator"], row["coverage"]["observed"]) == ("insufficient_sample", 0, 1)
    snapshot.projects[0].fields["builds"] = evidence([], EvidenceState.OBSERVED_EMPTY)
    snapshot.builds.clear()
    row = result(snapshot)
    assert (row["state"], row["denominator"], row["coverage"]) == (
        "insufficient_sample", 0, {"observed": 0, "expected": 0, "percent": None})


def test_missing_details_do_not_shrink_inventory():
    snapshot = make_snapshot()
    snapshot.builds.clear()
    doc = document(snapshot)
    row = doc["results"][0]
    assert doc["inventory_complete"] is False
    assert row["state"] == "missing" and row["coverage"] == {"observed": 0, "expected": 1, "percent": 0}
    assert doc["issues"][0]["code"] == "build_detail_missing"
    snapshot = make_snapshot()
    snapshot.repositories.clear()
    doc = document(snapshot)
    assert doc["results"][0]["state"] == "missing"
    assert doc["issues"][0]["code"] == "repository_detail_incomplete"


@pytest.mark.parametrize("state", [EvidenceState.PARTIAL, EvidenceState.UNAVAILABLE, EvidenceState.FAILED])
def test_unknown_discovery_is_not_an_exhaustive_partial_population(state):
    snapshot = make_snapshot()
    field = snapshot.projects[0].fields["builds"]
    snapshot.projects[0].fields["builds"] = replace(field, state=state)
    doc = document(snapshot)
    row = doc["results"][0]
    assert doc["inventory_complete"] is False
    assert row["state"] == ("collection_failed" if state == EvidenceState.FAILED else "unavailable")
    assert row["coverage"]["expected"] is None and row["value"] is None
    assert "secret-error-text" not in json.dumps(doc)


def test_known_sibling_and_scoped_duplicate_build_ids():
    first = make_snapshot()
    second = make_snapshot("p2", "r1", 7, 0, 20)
    first.projects.extend(second.projects)
    first.repositories.extend(second.repositories)
    first.builds.extend(second.builds)
    doc = document(first)
    assert [(r["scope"]["project"], r["value"]) for r in doc["results"]] == [("p1", 90), ("p2", 0)]
    second.projects[0].fields["builds"] = evidence(None, EvidenceState.FAILED)
    doc = document(first)
    assert doc["inventory_complete"] is False
    assert [(r["scope"]["project"], r["state"]) for r in doc["results"]] == [
        ("p1", "measured"), ("p2", "collection_failed")]
    assert doc["results"][1]["coverage"]["expected"] is None


@pytest.mark.parametrize("when,expected", [
    ("2026-01-01T00:00:00Z", "measured"),
    ("2026-02-01T00:00:00Z", "insufficient_sample"),
    ("2025-12-31T23:59:59Z", "insufficient_sample"),
    (None, "unavailable"),
])
def test_event_boundaries_and_unknown_time(when, expected):
    snapshot = make_snapshot()
    snapshot.projects[0].fields["builds"].value[0]["finishTime"] = when
    assert result(snapshot)["state"] == expected


@pytest.mark.parametrize("field,at,fresh", [
    ("builds", dt(20), True), ("builds", dt(2, 2), False),
    ("tests", dt(1), True), ("tests", dt(3, 2), True), ("tests", dt(2, 2), False),
])
def test_freshness_and_observation_cutoff(field, at, fresh):
    snapshot = make_snapshot()
    fields = snapshot.projects[0].fields if field == "builds" else snapshot.builds[0].fields
    fields[field] = replace(fields[field], observed_at=at, fresh_as_of=fresh)
    assert result(snapshot)["state"] == "unavailable"


@pytest.mark.parametrize("passed,total", [(21, 20), (True, 20), (1, False), (-1, 20), (1, -1), (None, 20)])
@pytest.mark.parametrize("event", ["2026-01-10T00:00:00Z", "2025-12-31T00:00:00Z"])
def test_malformed_counts_fail_even_outside_window(passed, total, event):
    snapshot = make_snapshot(passed=passed, total=total)
    snapshot.projects[0].fields["builds"].value[0]["finishTime"] = event
    row = result(snapshot)
    assert row["state"] == "collection_failed" and row["value"] is None
    assert row["coverage"]["observed"] == 0


@pytest.mark.parametrize("payload", [
    {"results": [{"passed": 1, "total": 2}], "unlinked_runs": 1, "linked_runs": 1},
    {"results": [{"passed": 1, "total": 2}], "unlinked_runs": 0},
    {"results": [], "unlinked_runs": 0, "linked_runs": 1},
    {"results": [], "unlinked_runs": False},
    {"results": [], "unlinked_runs": 0, "malformed_runs": True},
    {"results": [{"passed": 1, "total": 2}], "unlinked_runs": 0, "linked_runs": True},
    None,
])
def test_unlinked_malformed_and_presence_markers_never_fabricate_zero(payload):
    snapshot = make_snapshot()
    snapshot.builds[0].fields["tests"] = evidence(payload)
    row = result(snapshot)
    assert row["state"] in ("unavailable", "collection_failed")
    assert row["numerator"] is None and row["value"] is None


def test_duplicate_and_extra_details_and_unassignable_builds_are_visible():
    snapshot = make_snapshot()
    snapshot.builds.append(snapshot.builds[0])
    doc = document(snapshot)
    assert doc["results"][0]["state"] == "collection_failed"
    assert "duplicate_build_detail" in {i["code"] for i in doc["issues"]}
    snapshot = make_snapshot()
    snapshot.projects[0].fields["builds"].value.append({"id": 9, "repository": {}})
    doc = document(snapshot)
    assert doc["results"][0]["coverage"]["expected"] is None
    assert doc["results"][0]["state"] == "collection_failed"
    snapshot = make_snapshot()
    snapshot.builds.append(Record(Identity("connection", "p1", build="8"), {}))
    doc = document(snapshot)
    assert doc["results"][0]["state"] == "collection_failed"
    assert doc["results"][0]["coverage"]["expected"] is None


def test_no_project_identity_is_invented_and_empty_project_set_is_unknown():
    snapshot = make_snapshot()
    snapshot.projects[0].identity = Identity("connection", " ")
    doc = document(snapshot)
    assert doc["inventory_complete"] is False
    assert doc["results"] == []
    assert all(i["scope"]["project"] != " " for i in doc["issues"])
    snapshot.projects.clear()
    doc = document(snapshot)
    assert "project_inventory_unknown" in {i["code"] for i in doc["issues"]}


def test_malformed_scoped_record_blocks_only_its_project():
    snapshot = make_snapshot()
    sibling = make_snapshot("p2", "r2")
    snapshot.projects.extend(sibling.projects)
    snapshot.repositories.extend(sibling.repositories)
    snapshot.builds.extend(sibling.builds)
    snapshot.builds.append(Record(Identity("connection", "p1", build=" "), {}))
    doc = document(snapshot)
    assert [(row["scope"]["project"], row["state"]) for row in doc["results"]] == [
        ("p1", "collection_failed"), ("p2", "measured")]
    assert doc["results"][0]["coverage"]["expected"] is None
    assert any(issue["code"] == "malformed_record_identity" and issue["scope"]["project"] == "p1"
               for issue in doc["issues"])


def test_failed_build_evidence_is_not_discarded_outside_window():
    snapshot = make_snapshot()
    snapshot.projects[0].fields["builds"].value[0]["finishTime"] = "2025-12-31T00:00:00Z"
    snapshot.builds[0].fields["tests"] = evidence(None, EvidenceState.FAILED)
    assert result(snapshot)["state"] == "collection_failed"


@pytest.mark.parametrize("extra", [{"failed": True}, {"skipped": -1}, {"failed": 3}])
def test_supplied_outcome_counts_cannot_contradict_total(extra):
    snapshot = make_snapshot()
    snapshot.builds[0].fields["tests"].value["results"][0].update(extra)
    assert result(snapshot)["state"] == "collection_failed"


def test_missing_test_detail_and_failed_sibling_build():
    snapshot = make_snapshot()
    snapshot.builds[0].fields.pop("tests")
    assert result(snapshot)["state"] == "missing"
    snapshot = make_snapshot()
    sibling = make_snapshot("p2", "r2")
    sibling.builds[0].fields["tests"] = evidence(None, EvidenceState.FAILED)
    snapshot.projects.extend(sibling.projects)
    snapshot.repositories.extend(sibling.repositories)
    snapshot.builds.extend(sibling.builds)
    doc = document(snapshot)
    assert [row["state"] for row in doc["results"]] == ["measured", "collection_failed"]
    assert "secret-error-text" not in json.dumps(doc)


def test_missing_or_unavailable_build_field_and_malformed_historical_tests():
    snapshot = make_snapshot()
    snapshot.builds[0].fields.pop("build")
    assert result(snapshot)["state"] == "missing"
    snapshot.builds[0].fields["tests"].value["results"][0]["passed"] = True
    assert result(snapshot)["state"] == "collection_failed"
    snapshot = make_snapshot()
    snapshot.builds[0].fields["build"] = evidence(None, EvidenceState.UNAVAILABLE)
    assert result(snapshot)["state"] == "unavailable"
