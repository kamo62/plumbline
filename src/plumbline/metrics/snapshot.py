"""Pure, conservative projection of stored ADO-shaped snapshot evidence."""

from dataclasses import asdict, replace
from datetime import datetime
from enum import Enum

from plumbline.providers.contract import Evidence, EvidenceState, Identity, Record, Snapshot, Window

from .catalogue import CATALOGUE_VERSION, TEST_PASS_RATE
from .compute import calculate_test_pass_rate
from .model import Inventory, Issue, MetricResult, Observation, ResultState, TestCounts

SCHEMA_VERSION = "test-pass-rate-diagnostic/1"
NORMALISER_VERSION = "ado-build-test-summary/1"
CALCULATION_VERSION = "test-pass-rate/1"
POPULATION_POLICY = "ado-reported-total/v1"
POPULATION_DEFINITION = (
    "Sum reported passed and total executions from explicitly linked build test-run "
    "queries. Total includes all reported outcomes; no retry, skipped-outcome or "
    "logical-test deduplication is inferred. Each candidate is one build aggregate."
)
PARAMETER_KEYS = (
    "schema_version", "normaliser_version", "calculation_version", "catalogue_version",
    "definition", "population_policy", "population_definition", "minimum_sample",
)


def measurement_parameters(*, minimum_sample: int, population_policy: str) -> dict:
    if type(minimum_sample) is not int or minimum_sample < 1:
        raise ValueError("minimum_sample must be a positive integer")
    if population_policy != POPULATION_POLICY:
        raise ValueError("unsupported population policy")
    return {
        "schema_version": SCHEMA_VERSION,
        "normaliser_version": NORMALISER_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "catalogue_version": CATALOGUE_VERSION,
        "definition": asdict(TEST_PASS_RATE),
        "population_policy": POPULATION_POLICY,
        "population_definition": POPULATION_DEFINITION,
        "minimum_sample": minimum_sample,
    }


def _id(value: object) -> str | None:
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value and value == value.strip():
        return value
    return None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _usable(ev: object, window: Window, *, inventory: bool = False) -> bool:
    if (not isinstance(ev, Evidence) or not isinstance(ev.state, EvidenceState)
            or ev.state not in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY)):
        return False
    time = ev.observed_at
    return ((ev.fresh_as_of is None or ev.fresh_as_of is True)
            and isinstance(time, datetime) and time.tzinfo is not None and time.utcoffset() is not None
            and time <= window.as_of and (not inventory or time >= window.end))


def _state(ev: object) -> ResultState:
    if isinstance(ev, Evidence) and ev.state == EvidenceState.FAILED:
        return ResultState.COLLECTION_FAILED
    return ResultState.UNAVAILABLE


def _json(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _counts(ev: Evidence) -> tuple[EvidenceState, TestCounts | None, str | None]:
    """Validate the entire payload before event-window exclusion."""
    value = ev.value
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        return EvidenceState.FAILED, None, "malformed test summary"
    rows = value["results"]
    unlinked = value.get("unlinked_runs")
    linked = value.get("linked_runs")
    malformed = value.get("malformed_runs", False)
    if (type(unlinked) is not int or unlinked < 0
            or (linked is not None and (type(linked) is not int or linked < 0))
            or type(malformed) is not bool):
        return EvidenceState.FAILED, None, "malformed test summary markers"
    if malformed:
        return EvidenceState.FAILED, None, "malformed test summary"
    passed = total = 0
    for row in rows:
        if not isinstance(row, dict):
            return EvidenceState.FAILED, None, "malformed test counts"
        p, t = row.get("passed"), row.get("total")
        if type(p) is not int or type(t) is not int or p < 0 or t < 0 or p > t:
            return EvidenceState.FAILED, None, "invalid test counts"
        others = [row.get(key) for key in ("failed", "skipped", "not_executed")]
        if (any(count is not None and (type(count) is not int or count < 0) for count in others)
                or p + sum(count for count in others if count is not None) > t):
            return EvidenceState.FAILED, None, "invalid test counts"
        passed += p
        total += t
    if unlinked:
        return EvidenceState.PARTIAL, None, "unlinked test runs"
    if rows:
        if ev.state == EvidenceState.OBSERVED_EMPTY:
            return EvidenceState.FAILED, None, "invalid empty test summary"
        if linked is None or linked < 1 or linked < len(rows):
            return EvidenceState.PARTIAL, None, "test run presence unknown"
        return EvidenceState.OBSERVED, TestCounts(passed, total), None
    if linked not in (None, 0) or ev.state != EvidenceState.OBSERVED_EMPTY:
        return EvidenceState.PARTIAL, None, "test run presence unknown"
    return EvidenceState.OBSERVED_EMPTY, None, None


def _add_issue(result: MetricResult, state: ResultState, detail: str) -> MetricResult:
    issues = result.issues + (Issue(None, state, detail),)
    priority = (ResultState.COLLECTION_FAILED, ResultState.UNAVAILABLE, ResultState.MISSING)
    selected = next(item for item in priority if any(issue.state == item for issue in issues))
    return replace(result, state=selected, numerator=None, denominator=None, value=None, issues=issues)


def build_test_pass_rate_document(
    snapshot: Snapshot, snapshot_id: str, *, minimum_sample: int, population_policy: str,
) -> dict:
    parameters = measurement_parameters(minimum_sample=minimum_sample, population_policy=population_policy)
    if (not isinstance(snapshot, Snapshot) or not isinstance(snapshot.window, Window)
            or not isinstance(snapshot.connection, str) or _id(snapshot.connection) != snapshot.connection
            or not isinstance(snapshot_id, str)
            or not snapshot_id.strip()):
        raise ValueError("valid snapshot, connection and snapshot_id required")
    window = snapshot.window
    issues: list[dict] = []
    results = []

    def problem(code: str, state: ResultState, project: str | None = None,
                repository: str | None = None, build: str | None = None) -> None:
        issues.append({"code": code, "state": state.value, "scope": {
            "connection": snapshot.connection, "project": project, "repository": repository, "build": build,
        }})

    def index(records: object, kind: str) -> dict:
        indexed: dict = {}
        if not isinstance(records, list):
            problem("malformed_record_inventory", ResultState.COLLECTION_FAILED)
            return indexed
        for record in records:
            identity = record.identity if isinstance(record, Record) else None
            if (not isinstance(identity, Identity) or identity.connection != snapshot.connection
                    or not isinstance(identity.project, str) or _id(identity.project) != identity.project
                    or not isinstance(record.fields, dict)
                    or (kind == "project" and (identity.repository is not None or identity.build is not None))
                    or (kind == "repository" and (_id(identity.repository) != identity.repository
                                                   or identity.repository is None or identity.build is not None))
                    or (kind == "build" and (_id(identity.build) != identity.build or identity.build is None
                                             or (identity.repository is not None and
                                                 _id(identity.repository) != identity.repository)))):
                known_project = (identity.project if isinstance(identity, Identity)
                                 and identity.connection == snapshot.connection
                                 and isinstance(identity.project, str)
                                 and _id(identity.project) == identity.project else None)
                problem("malformed_record_identity", ResultState.COLLECTION_FAILED, known_project)
                continue
            key = (identity.project,) if kind == "project" else (
                (identity.project, identity.repository) if kind == "repository" else (identity.project, identity.build))
            indexed.setdefault(key, []).append(record)
        return indexed

    projects = index(snapshot.projects, "project")
    repos = index(snapshot.repositories, "repository")
    builds = index(snapshot.builds, "build")
    if not snapshot.projects:
        problem("project_inventory_unknown", ResultState.UNAVAILABLE)
    known_repos: set[tuple] = set()
    known_builds: set[tuple] = set()

    for (pid,), project_records in sorted(projects.items()):
        if len(project_records) != 1:
            problem("duplicate_project", ResultState.COLLECTION_FAILED, pid)
            continue
        project = project_records[0]
        project_ev = project.fields.get("project")
        project_ok = (_usable(project_ev, window) and isinstance(project_ev.value, dict)
                      and _id(project_ev.value.get("id")) == pid)
        if not project_ok:
            problem("project_evidence_incomplete", _state(project_ev), pid)
        repo_ev = project.fields.get("repositories")
        build_ev = project.fields.get("builds")
        repo_items = repo_ev.value if isinstance(repo_ev, Evidence) and isinstance(repo_ev.value, list) else []
        build_items = build_ev.value if isinstance(build_ev, Evidence) and isinstance(build_ev.value, list) else []
        repo_ids: set[str] = set()
        repo_malformed = isinstance(repo_ev, Evidence) and repo_ev.value is not None and not isinstance(repo_ev.value, list)
        repo_bad = not _usable(repo_ev, window, inventory=True) or not isinstance(getattr(repo_ev, "value", None), list)
        if isinstance(repo_ev, Evidence) and repo_ev.state == EvidenceState.OBSERVED_EMPTY and repo_items:
            repo_bad = True
            repo_malformed = True
        for item in repo_items:
            rid = _id(item.get("id")) if isinstance(item, dict) else None
            if rid is None or rid in repo_ids:
                repo_bad = True
                repo_malformed = True
            else:
                repo_ids.add(rid)
        if repo_bad:
            problem("repository_inventory_incomplete", ResultState.COLLECTION_FAILED if repo_malformed else _state(repo_ev), pid)

        candidates: dict[str, dict[str, dict]] = {rid: {} for rid in repo_ids}
        seen_builds: set[str] = set()
        build_malformed = isinstance(build_ev, Evidence) and build_ev.value is not None and not isinstance(build_ev.value, list)
        build_bad = not _usable(build_ev, window, inventory=True) or not isinstance(getattr(build_ev, "value", None), list)
        if isinstance(build_ev, Evidence) and build_ev.state == EvidenceState.OBSERVED_EMPTY and build_items:
            build_bad = True
            build_malformed = True
        for item in build_items:
            bid = _id(item.get("id")) if isinstance(item, dict) else None
            repository = item.get("repository") if isinstance(item, dict) else None
            rid = _id(repository.get("id")) if isinstance(repository, dict) else None
            if bid is None or rid is None or bid in seen_builds or rid not in repo_ids:
                build_bad = True
                build_malformed = True
                continue
            seen_builds.add(bid)
            candidates[rid][bid] = item
            known_builds.add((pid, bid))
        if build_bad:
            problem("build_inventory_incomplete", ResultState.COLLECTION_FAILED if build_malformed else _state(build_ev), pid)

        for rid in sorted(repo_ids):
            known_repos.add((pid, rid))
            records = repos.get((pid, rid), [])
            repo_detail_ok = len(records) == 1
            repo_detail_state = ResultState.MISSING if not records else ResultState.COLLECTION_FAILED
            if repo_detail_ok:
                rev = records[0].fields.get("repository")
                repo_detail_ok = (_usable(rev, window) and isinstance(rev.value, dict)
                                  and _id(rev.value.get("id")) == rid)
                repo_detail_state = _state(rev)
            if not repo_detail_ok:
                problem("repository_detail_incomplete", repo_detail_state, pid, rid)

            observations: list[Observation[TestCounts]] = []
            for bid, item in sorted(candidates[rid].items()):
                detail = builds.get((pid, bid), [])
                if not detail:
                    problem("build_detail_missing", ResultState.MISSING, pid, rid, bid)
                    continue
                if len(detail) != 1:
                    problem("duplicate_build_detail", ResultState.COLLECTION_FAILED, pid, rid, bid)
                    observations.append(Observation(bid, None, EvidenceState.FAILED, None, None, "duplicate build detail"))
                    continue
                record = detail[0]
                bev = record.fields.get("build")
                tev = record.fields.get("tests")
                state: EvidenceState
                detail_text: str | None
                if not isinstance(bev, Evidence):
                    problem("build_detail_missing", ResultState.MISSING, pid, rid, bid)
                    if isinstance(tev, Evidence) and tev.value is not None:
                        test_state, _, test_detail = _counts(tev)
                        if test_state == EvidenceState.FAILED and test_detail:
                            observations.append(Observation(bid, _time(item.get("finishTime")), test_state,
                                                            None, None, test_detail))
                    continue
                raw = bev.value if isinstance(bev, Evidence) and isinstance(bev.value, dict) else {}
                raw_repo = raw.get("repository")
                event = _time(item.get("finishTime"))
                if bev.value is None and bev.state in (EvidenceState.FAILED, EvidenceState.PARTIAL, EvidenceState.UNAVAILABLE):
                    state = EvidenceState.FAILED if bev.state == EvidenceState.FAILED else EvidenceState.PARTIAL
                    detail_text = "build evidence incomplete"
                    if isinstance(tev, Evidence) and tev.value is not None:
                        test_state, _, test_detail = _counts(tev)
                        if test_state == EvidenceState.FAILED and test_detail:
                            state, detail_text = test_state, test_detail
                    problem("build_detail_incomplete", ResultState.COLLECTION_FAILED if state == EvidenceState.FAILED
                            else ResultState.UNAVAILABLE, pid, rid, bid)
                    observations.append(Observation(bid, event, state, None, None, detail_text))
                    continue
                consistent = (_id(raw.get("id")) == bid and isinstance(raw_repo, dict)
                              and _id(raw_repo.get("id")) == rid
                              and (record.identity.repository is None or record.identity.repository == rid)
                              and _time(raw.get("finishTime")) == event
                              and raw.get("status") == item.get("status")
                              and raw.get("result") == item.get("result"))
                if not consistent:
                    problem("build_detail_mismatch", ResultState.COLLECTION_FAILED, pid, rid, bid)
                    observations.append(Observation(bid, event, EvidenceState.FAILED, None, None, "build detail mismatch"))
                    continue
                if not isinstance(tev, Evidence):
                    problem("test_detail_missing", ResultState.MISSING, pid, rid, bid)
                    continue
                # Check supplied payloads even when the field or event is otherwise unusable.
                count_state, counts, count_detail = (
                    _counts(tev) if tev.value is not None or tev.state in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY)
                    else (tev.state, None, None)
                )
                if count_state == EvidenceState.FAILED and count_detail:
                    state, detail_text = count_state, count_detail
                elif not _usable(bev, window) or not _usable(tev, window):
                    state = EvidenceState.FAILED if _state(bev) == ResultState.COLLECTION_FAILED or _state(tev) == ResultState.COLLECTION_FAILED else EvidenceState.PARTIAL
                    detail_text = "build or test evidence incomplete"
                elif (not isinstance(item.get("status"), str) or item["status"].lower() != "completed"
                      or not isinstance(item.get("result"), str) or not item["result"].strip()):
                    state, detail_text = EvidenceState.PARTIAL, "build completion unknown"
                else:
                    state, detail_text = count_state, count_detail
                observed = (min(bev.observed_at, tev.observed_at)
                            if _usable(bev, window) and _usable(tev, window)
                            and bev.observed_at is not None and tev.observed_at is not None else None)
                observations.append(Observation(bid, event, state, observed, counts, detail_text))

            expected = None if build_bad or repo_bad or not project_ok else tuple(sorted(candidates[rid]))
            failed_inventory = (repo_malformed or build_malformed or any(
                _state(ev) == ResultState.COLLECTION_FAILED for ev in (project_ev, repo_ev, build_ev)))
            inv_state = (EvidenceState.FAILED if failed_inventory else EvidenceState.PARTIAL) if expected is None else (
                EvidenceState.OBSERVED if expected else EvidenceState.OBSERVED_EMPTY)
            observed_at = build_ev.observed_at if isinstance(build_ev, Evidence) else None
            result = calculate_test_pass_rate(Identity(snapshot.connection, pid, rid), window,
                                              Inventory(inv_state, observed_at, expected, tuple(observations)), minimum_sample)
            if not repo_detail_ok:
                result = _add_issue(result, repo_detail_state, "repository detail incomplete")
            results.append(result)

    for pid, rid in sorted(set(repos) - known_repos):
        problem("unexpected_repository_detail", ResultState.COLLECTION_FAILED, pid, rid)
    for pid, bid in sorted(set(builds) - known_builds):
        problem("unexpected_build_detail", ResultState.COLLECTION_FAILED, pid, build=bid)

    # Unexpected details can indicate an incomplete discovery population for their project.
    affected = {issue["scope"]["project"] for issue in issues
                if issue["code"] in ("unexpected_repository_detail", "unexpected_build_detail",
                                     "malformed_record_identity")}
    for i, result in enumerate(results):
        if result.scope.project in affected:
            result = _add_issue(result, ResultState.COLLECTION_FAILED, "malformed or unexpected scoped record")
            result = replace(result, coverage=replace(result.coverage, expected=None))
            results[i] = result
    issues.sort(key=lambda issue: (issue["scope"]["project"] or "", issue["scope"]["repository"] or "",
                                   issue["scope"]["build"] or "", issue["code"], issue["state"]))
    return {**parameters, "snapshot_id": snapshot_id, "window": _json(asdict(window)),
            "inventory_complete": not issues, "issues": issues,
            "results": [_json({**asdict(result), "coverage": {**asdict(result.coverage),
                                                                "percent": result.coverage.percent}})
                        for result in results]}
