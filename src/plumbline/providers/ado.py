"""Adapter over the existing Azure DevOps client; no HTTP implementation here."""
import asyncio
from datetime import datetime, timezone
from dataclasses import replace
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
    def __init__(self, client: Any, connection_id: str, *, clock: Callable[[], datetime] | None = None, progress_callback=None):
        if not connection_id:
            raise ValueError("connection_id is required")
        self.client = client
        self.connection_id = connection_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.progress_callback = progress_callback

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
        if not isinstance(evidence.value, list):
            return evidence
        selected, unknown = [], []
        for item in evidence.value:
            if not isinstance(item, dict):
                unknown.append(item)
                continue
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
        return replace(evidence, value=selected + unknown,
                       state=EvidenceState.PARTIAL if unknown or evidence.state == EvidenceState.PARTIAL else (EvidenceState.OBSERVED if selected else EvidenceState.OBSERVED_EMPTY),
                       error_code="unknown_event_time" if unknown else evidence.error_code)

    async def collect(self, projects: list[str], window: Window) -> Snapshot:
        snapshot = Snapshot(self.connection_id, window, self.capabilities)
        seen = set()
        for requested in dict.fromkeys(projects):
            if requested in seen:
                continue
            project = await self._call("get_project", (requested,), dict, window)
            pid = project.value.get("id") if isinstance(project.value, dict) else None
            if not pid or not isinstance(project.value.get("name"), str) or not project.value["name"].strip():
                snapshot.projects.append(Record(Identity(self.connection_id, requested), {"project": replace(project, state=EvidenceState.FAILED if project.state == EvidenceState.FAILED else EvidenceState.PARTIAL, error_code=project.error_code or "missing_identity")}))
                continue
            pid = str(pid)
            if pid in seen:
                continue
            seen.add(pid)
            identity = Identity(self.connection_id, pid)
            pr = Record(identity, {"project": project})
            snapshot.projects.append(pr)
            repos = await self._call("get_repositories", (requested,), list, window)
            if hasattr(self.client, "get_builds_window"):
                builds = await self._call("get_builds_window", (requested, window.start, window.end, self.clock()), list, window)
            else:
                builds = await self._call("get_builds", (requested, None, None), list, window)
                if isinstance(builds.value, list) and builds.observed_at >= window.end:
                    builds = replace(builds, value=[b for b in builds.value if not (isinstance(b, dict) and b.get("finishTime") is None and str(b.get("status", "")).lower() in {"inprogress", "notstarted", "postponed", "cancelling"})])
            pr.fields.update(repositories=repos, builds=self._events(builds, ("finishTime",), window))
            policy_configs, policy_types = await asyncio.gather(
                self._call("get_policy_configurations", (requested,), list, window),
                self._call("get_policy_types", (requested,), list, window),
            )
            pr.fields.update(policy_configurations=policy_configs, policy_types=policy_types)
            if isinstance(repos.value, list):
                for repo in repos.value:
                    if not isinstance(repo, dict):
                        continue
                    rid = repo.get("id")
                    if not rid or not isinstance(repo.get("name"), str) or not repo["name"].strip():
                        pr.fields["repositories"] = replace(repos, state=EvidenceState.PARTIAL, error_code="missing_identity")
                        continue
                    if repo.get("isDisabled"):
                        continue
                    progress = {"project_id": pid, "project_name": requested,
                                "repository_id": str(rid), "repository_name": repo.get("name", str(rid)),
                                "status": "running", "errors": []}
                    if self.progress_callback:
                        self.progress_callback(progress.copy())
                    record = Record(Identity(self.connection_id, pid, str(rid)), {"repository": replace(repos, value=repo, state=EvidenceState.OBSERVED)})
                    snapshot.repositories.append(record)
                    for field, method, args in (
                        ("commits", "get_commits", (str(rid), requested, window.start.isoformat(), window.end.isoformat())),
                        ("branches", "get_branches", (str(rid), requested)),
                    ):
                        ev = await self._call(method, args, list, window)
                        record.fields[field] = ev
                    if hasattr(self.client, "get_pull_requests_window"):
                        ev = await self._call("get_pull_requests_window", (requested, str(rid), window.start, window.end), list, window)
                    else:
                        ev = await self._call("get_pull_requests", (requested, str(rid), None), list, window)
                    ev = self._events(ev, ("creationDate", "closedDate"), window)
                    if isinstance(ev.value, list) and any(isinstance(x, dict) and x.get("collection_failures") for x in ev.value):
                        ev = replace(ev, state=EvidenceState.PARTIAL, error_code="enrichment_failed")
                    if isinstance(ev.value, list) and any(isinstance(x, dict) and (t := _time(x.get("closedDate"))) is not None and t >= window.end for x in ev.value):
                        ev = replace(ev, state=EvidenceState.PARTIAL, error_code="historical_pr_status_unknown")
                    record.fields["pull_requests"] = ev
                    errors = [{"field": name, "error": ev.error_code} for name, ev in {**record.fields, "policy_configurations": policy_configs, "policy_types": policy_types}.items()
                              if ev.state not in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY)]
                    if not errors:
                        try:
                            from plumbline.collection.projection import _policies
                            if not repo.get("defaultBranch") and record.fields["branches"].state == EvidenceState.OBSERVED and record.fields["branches"].value:
                                raise ValueError("unknown_default_branch")
                            _policies(policy_configs.value, policy_types.value, str(rid), policy_configs.observed_at, repo.get("defaultBranch"), empty_repo=(record.fields["branches"].state == EvidenceState.OBSERVED_EMPTY))
                        except (ValueError, TypeError, AttributeError) as exc:
                            errors.append({"field": "policies", "error": str(exc)})
                    if self.progress_callback:
                        self.progress_callback({**progress, "status": "failed" if errors else "succeeded", "errors": errors})
            selected = pr.fields["builds"]
            if isinstance(selected.value, list):
                for build in selected.value:
                    if not isinstance(build, dict):
                        continue
                    bid = build.get("id")
                    if bid is None or not isinstance(build.get("repository"), dict) or not build["repository"].get("id"):
                        pr.fields["builds"] = replace(selected, state=EvidenceState.PARTIAL, error_code="missing_identity")
                        continue
                    record = Record(Identity(self.connection_id, pid, build=str(bid)), {"build": replace(selected, value=build, state=EvidenceState.PARTIAL if build.get("enrichment_failures") or not _time(build.get("finishTime")) or str(build.get("status", "")).lower() != "completed" or not isinstance(build.get("result"), str) else EvidenceState.OBSERVED, error_code="incomplete_build" if not _time(build.get("finishTime")) or str(build.get("status", "")).lower() != "completed" or not isinstance(build.get("result"), str) else ("enrichment_failed" if build.get("enrichment_failures") else None))})
                    snapshot.builds.append(record)
                    methods = (("coverage", "get_code_coverage", dict), ("tests", "get_test_results", dict), ("security_scans", "get_security_scan_results", list))
                    outcomes = await asyncio.gather(*(self._call(method, (requested, bid), expected, window) for _, method, expected in methods))
                    for (field, method, expected), ev in zip(methods, outcomes):
                        if field == "tests" and isinstance(ev.value, dict):
                            rows = ev.value.get("results")
                            valid = isinstance(rows, list) and all(
                                isinstance(row, dict) and all(type(row.get(k)) is int and row[k] >= 0 for k in ("total", "passed"))
                                and row["passed"] <= row["total"]
                                and all(row.get(k) is None or (type(row[k]) is int and row[k] >= 0) for k in ("failed", "skipped", "not_executed"))
                                and sum(row.get(k) or 0 for k in ("passed", "failed", "skipped", "not_executed")) <= row["total"]
                                for row in rows)
                            unlinked = ev.value.get("unlinked_runs", 0)
                            if type(unlinked) is not int or unlinked < 0:
                                valid = False
                            if not valid or ev.value.get("malformed_runs"):
                                ev = replace(ev, state=EvidenceState.PARTIAL, error_code="malformed_shape")
                            elif ev.value.get("unlinked_runs"):
                                ev = replace(ev, state=EvidenceState.PARTIAL, error_code="unlinked_test_runs")
                            elif not rows and ev.value.get("unlinked_runs") == 0:
                                ev = replace(ev, state=EvidenceState.OBSERVED_EMPTY)
                            elif not rows:
                                ev = replace(ev, state=EvidenceState.PARTIAL, error_code="test_run_presence_unknown")
                            elif all(row["total"] == 0 for row in rows) and not ev.value.get("linked_runs"):
                                ev = replace(ev, state=EvidenceState.PARTIAL, error_code="test_run_presence_unknown")
                        if field == "coverage" and isinstance(ev.value, dict) and ev.value:
                            try:
                                from plumbline.collection.projection import _coverage
                                projected = _coverage(ev.value)
                                if ev.value.get("lastError") or any(item.get("lastError") for item in projected.get("value", [])):
                                    ev = replace(ev, state=EvidenceState.PARTIAL, error_code="coverage_unavailable")
                            except ValueError:
                                ev = replace(ev, state=EvidenceState.PARTIAL, error_code="malformed_coverage")
                        if field == "security_scans" and ev.state in (EvidenceState.OBSERVED, EvidenceState.OBSERVED_EMPTY):
                            ev = replace(ev, state=EvidenceState.PARTIAL, error_code="artifact_name_heuristic")
                        record.fields[field] = ev
        return snapshot
