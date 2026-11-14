"""Adapter over the existing Azure DevOps client; no HTTP implementation here."""
from datetime import datetime, timezone
from typing import Any, Callable

from .contract import Evidence, EvidenceState, Identity, Record, Snapshot, Window

CAPABILITIES = frozenset({"projects", "repositories", "commits", "branches", "pull_requests", "policies", "builds", "tests", "coverage", "security_scans"})


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


class ADOProvider:
    def __init__(self, client: Any, connection_id: str, *, clock: Callable[[], datetime] | None = None):
        if not connection_id:
            raise ValueError("connection_id is required")
        self.client = client
        self.connection_id = connection_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES

    def _evidence(self, value: Any, source: str, window: Window, state: EvidenceState | None = None, code: str | None = None, detail: str | None = None) -> Evidence:
        now = self.clock()
        return Evidence(value, state or (EvidenceState.OBSERVED_EMPTY if value == [] or value == {} else EvidenceState.OBSERVED), source, now, code, detail, now <= window.as_of)

    async def _call(self, method: str, args: tuple, expected: type, window: Window) -> Evidence:
        try:
            value = await getattr(self.client, method)(*args)
        except Exception as exc:
            status = getattr(exc, "status", None)
            code = "permission_denied" if status in (401, 403) else "request_failed"
            return self._evidence(None, method, window, EvidenceState.FAILED, code, type(exc).__name__)
        if not isinstance(value, expected) or (expected is list and any(not isinstance(item, dict) for item in value)):
            return self._evidence(value, method, window, EvidenceState.PARTIAL, "malformed_shape")
        return self._evidence(value, method, window)

    def _events(self, evidence: Evidence, keys: tuple[str, ...], window: Window) -> Evidence:
        if evidence.state not in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY):
            return evidence
        selected, unknown = [], []
        for item in evidence.value:
            dates = [_time(item.get(key)) for key in keys if item.get(key) is not None]
            if not dates or any(date is None for date in dates):
                unknown.append(item)
            elif keys == ("creationDate", "closedDate"):
                created = _time(item.get("creationDate"))
                closed = _time(item.get("closedDate"))
                if created is None:
                    unknown.append(item)
                elif created < window.end and (closed is None or closed >= window.start):
                    selected.append(item)
            elif any(window.start <= date < window.end for date in dates):
                selected.append(item)
        return self._evidence(selected + unknown, evidence.source, window,
                              EvidenceState.PARTIAL if unknown else None,
                              "unknown_event_time" if unknown else None)

    async def collect(self, projects: list[str], window: Window) -> Snapshot:
        snapshot = Snapshot(self.connection_id, window, self.capabilities)
        for requested in dict.fromkeys(projects):
            project = await self._call("get_project", (requested,), dict, window)
            pid = project.value.get("id") if isinstance(project.value, dict) else None
            if not pid:
                snapshot.projects.append(Record(Identity(self.connection_id, requested), {"project": self._evidence(project.value, "get_project", window, EvidenceState.FAILED if project.state == EvidenceState.FAILED else EvidenceState.PARTIAL, project.error_code or "missing_identity")}))
                continue
            pid = str(pid)
            identity = Identity(self.connection_id, pid)
            pr = Record(identity, {"project": project})
            snapshot.projects.append(pr)
            repos = await self._call("get_repositories", (requested,), list, window)
            builds = await self._call("get_builds", (requested, None, window.start.isoformat()), list, window)
            pr.fields.update(repositories=repos, builds=self._events(builds, ("queueTime",), window))
            if repos.state in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY):
                for repo in repos.value:
                    rid = repo.get("id")
                    if not rid:
                        pr.fields["repositories"] = self._evidence(repos.value, "get_repositories", window, EvidenceState.PARTIAL, "missing_identity")
                        continue
                    record = Record(Identity(self.connection_id, pid, str(rid)), {"repository": self._evidence(repo, "get_repositories", window)})
                    snapshot.repositories.append(record)
                    for field, method, args in (
                        ("commits", "get_commits", (str(rid), requested, window.start.isoformat(), window.end.isoformat())),
                        ("branches", "get_branches", (str(rid), requested)),
                        ("pull_requests", "get_pull_requests", (requested, str(rid), None)),
                        ("policies", "get_branch_policies", (requested, str(rid))),
                    ):
                        ev = await self._call(method, args, list, window)
                        if field == "pull_requests":
                            ev = self._events(ev, ("creationDate", "closedDate"), window)
                            if isinstance(ev.value, list) and any(x.get("collection_failures") for x in ev.value):
                                ev = self._evidence(ev.value, method, window, EvidenceState.PARTIAL, "enrichment_failed")
                        record.fields[field] = ev
            selected = pr.fields["builds"]
            if isinstance(selected.value, list):
                for build in selected.value:
                    bid = build.get("id")
                    if bid is None:
                        pr.fields["builds"] = self._evidence(selected.value, "get_builds", window, EvidenceState.PARTIAL, "missing_identity")
                        continue
                    record = Record(Identity(self.connection_id, pid, build=str(bid)), {"build": self._evidence(build, "get_builds", window, EvidenceState.PARTIAL if build.get("enrichment_failures") else None, "enrichment_failed" if build.get("enrichment_failures") else None)})
                    snapshot.builds.append(record)
                    for field, method, expected in (("coverage", "get_code_coverage", dict), ("tests", "get_test_results", dict), ("security_scans", "get_security_scan_results", list)):
                        ev = await self._call(method, (requested, bid), expected, window)
                        if field == "tests" and isinstance(ev.value, dict) and ev.state == EvidenceState.OBSERVED and not isinstance(ev.value.get("results"), list):
                            ev = self._evidence(ev.value, method, window, EvidenceState.PARTIAL, "malformed_shape")
                        if field == "tests" and isinstance(ev.value, dict) and ev.value.get("results") and all(isinstance(row, dict) and row.get("total") == 0 for row in ev.value["results"]):
                            ev = self._evidence(ev.value, method, window, EvidenceState.PARTIAL, "test_run_presence_unknown")
                        if field == "security_scans" and ev.state in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY):
                            ev = self._evidence(ev.value, method, window, EvidenceState.PARTIAL, "artifact_name_heuristic")
                        if field == "tests" and isinstance(ev.value, dict) and ev.value.get("unlinked_runs"):
                            ev = self._evidence(ev.value, method, window, EvidenceState.PARTIAL, "unlinked_test_runs")
                        record.fields[field] = ev
        return snapshot
