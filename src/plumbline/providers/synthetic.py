"""Deterministic, fictional records for offline provider exercises."""
from datetime import datetime, timezone

from .contract import Evidence, EvidenceState, Identity, Record, Snapshot, Window
from .ado import CAPABILITIES

OBSERVED_AT = datetime(2026, 2, 2, tzinfo=timezone.utc)
WINDOW = Window(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc), OBSERVED_AT)

# Both projects intentionally have build ID 7. PR 11 opened before the window.
DATA = {
    "Alpha": {"id": "p1", "repos": [{"id": "r1", "name": "one"}, {"id": "r2", "name": "two"}], "prs": {"r1": [{"pullRequestId": 11, "creationDate": "2025-12-20T00:00:00Z", "closedDate": "2026-01-10T00:00:00Z", "createdBy": {"id": "author"}, "reviewers": [{"id": "reviewer", "vote": 10}]}], "r2": []}, "build": {"id": 7, "queueTime": "2026-01-15T00:00:00Z", "repository": {"id": "r1"}}, "tests": {"results": [{"total": 10, "passed": 8, "failed": 2, "skipped": 0, "not_executed": 0}], "unlinked_runs": 1}, "coverage": {"value": [{"lineCoverage": 80}]}, "security": [{"name": "scan", "result": "failed"}]},
    "Beta": {"id": "p2", "repos": [{"id": "r3", "name": "three"}], "prs": {"r3": [{"pullRequestId": 12, "creationDate": "2026-01-20T00:00:00Z", "createdBy": {"id": "other"}, "reviewers": []}]}, "build": {"id": 7, "queueTime": "2026-01-25T00:00:00Z", "repository": {"id": "r3"}}, "tests": None, "coverage": {}, "security": []},
}


class SyntheticProvider:
    """Deterministic source with deliberately partial and failed evidence."""
    def __init__(self, connection_id: str = "synthetic"):
        self.connection_id = connection_id

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES

    async def collect(self, projects: list[str], window: Window) -> Snapshot:
        if window != WINDOW:
            raise ValueError("synthetic dataset supports its documented window only")
        result = Snapshot(self.connection_id, window, self.capabilities)
        def ev(value, source, state=None, code=None):
            return Evidence(value, state or (EvidenceState.OBSERVED_EMPTY if value == [] or value == {} else EvidenceState.OBSERVED), source, OBSERVED_AT, code, None, True)
        for name in dict.fromkeys(projects):
            if name not in DATA:
                result.projects.append(Record(Identity(self.connection_id, name), {"project": ev(None, "get_project", EvidenceState.FAILED, "request_failed")}))
                continue
            item = DATA[name]
            pid = item["id"]
            project = {"id": pid, "name": name}
            result.projects.append(Record(Identity(self.connection_id, pid), {"project": ev(project, "get_project"), "repositories": ev(item["repos"], "get_repositories"), "builds": ev([item["build"]], "get_builds")}))
            for repo in item["repos"]:
                rid = repo["id"]
                result.repositories.append(Record(Identity(self.connection_id, pid, rid), {"repository": ev(repo, "get_repositories"), "commits": ev([], "get_commits"), "branches": ev([], "get_branches"), "pull_requests": ev(item["prs"][rid], "get_pull_requests"), "policies": ev([], "get_branch_policies")}))
            tests = ev(item["tests"], "get_test_results", EvidenceState.PARTIAL if name == "Alpha" else EvidenceState.FAILED, "unlinked_test_runs" if name == "Alpha" else "request_failed")
            if name == "Beta":
                tests = Evidence(None, EvidenceState.FAILED, "get_test_results", OBSERVED_AT, "request_failed", "RuntimeError", True)
            result.builds.append(Record(Identity(self.connection_id, pid, build="7"), {"build": ev(item["build"], "get_builds"), "tests": tests, "coverage": ev(item["coverage"], "get_code_coverage"), "security_scans": ev(item["security"], "get_security_scan_results", EvidenceState.PARTIAL, "artifact_name_heuristic")}))
        return result
