"""Builds-related API client extracted from AzureDevOpsClient."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, cast, TYPE_CHECKING
import asyncio
import aiohttp

from .errors import ADORequestError

if TYPE_CHECKING:
    from .ado_client import AzureDevOpsClient


class BuildsClient:
    """Subset client that handles builds, coverage, test results, and security artifacts."""

    def __init__(self, parent: "AzureDevOpsClient") -> None:
        self._parent = parent

    async def get_security_scan_results(self, project: str, build_id: int) -> List[Dict[str, Any]]:
        try:
            session = await self._parent._get_aiohttp_session()
            url = f"{self._parent.base_url}/{project}/_apis/build/builds/{build_id}/artifacts"
            params = {"api-version": "7.1"}
            artifacts = await self._parent.get_paginated_results(url, session, params)
            security_artifacts: List[Dict[str, Any]] = []
            for artifact in artifacts:
                name = str(artifact.get("name", "")).lower()
                if any(tok in name for tok in ("security", "sarif", "codeanalysis")):
                    security_artifacts.append(artifact)
            if not security_artifacts:
                self._parent.logger.debug(f"No security scan artifacts found for build {build_id}")
            return security_artifacts
        except ADORequestError:
            raise
        except aiohttp.ClientError as e:
            raise ADORequestError(f"Failed to get security scan results for build {build_id}: {e}") from e
        except Exception as e:
            raise ADORequestError(f"Unexpected error getting security scan results: {e}") from e

    async def get_builds(
        self, project: str, project_name: Optional[str] = None, from_date: Optional[str] = None, *, candidate=None
    ) -> List[Dict[str, Any]]:
        try:
            project_name = project_name or project
            session = await self._parent._get_aiohttp_session()
            url = f"{self._parent.base_url}/{project_name}/_apis/build/builds"
            params: Dict[str, Any] = {"api-version": "7.1"}
            if from_date:
                params["minTime"] = from_date
                params["queryOrder"] = "finishTimeDescending"
                self._parent.logger.info(f"Filtering builds completed after {from_date}")

            builds = await self._parent.get_paginated_results(url, session, params)
            if candidate is not None:
                builds = [build for build in builds if candidate(build)]
            detailed_builds: List[Dict[str, Any]] = []

            for build in builds:
                if not isinstance(build, dict):
                    detailed_builds.append(build)
                    continue
                try:
                    build_id = build["id"]
                    self._parent.logger.debug(f"Processing build {build_id} for {project_name}")

                    # Timeline
                    timeline_url = f"{self._parent.base_url}/{project_name}/_apis/build/builds/{build_id}/timeline"
                    timeline_params = {"api-version": "7.1"}
                    try:
                        build["timeline"], _ = await self._parent._request_json(timeline_url, session, timeline_params)
                    except (ADORequestError, aiohttp.ClientError) as e:
                        build.setdefault("enrichment_failures", {})["timeline"] = str(e)
                        build["timeline"] = {}
                        self._parent.logger.debug(f"No timeline found for build {build_id}")

                    # Changes
                    changes_url = f"{self._parent.base_url}/{project_name}/_apis/build/builds/{build_id}/changes"
                    changes_params = {"api-version": "7.1"}
                    try:
                        build["changes"] = await self._parent.get_paginated_results(changes_url, session, changes_params)
                    except (ADORequestError, aiohttp.ClientError) as e:
                        build["changes"] = []
                        build.setdefault("enrichment_failures", {})["changes"] = str(e)
                        self._parent.logger.debug(f"Failed to get changes for build {build_id}: {e}")

                    detailed_builds.append(build)
                except ADORequestError as e:
                    self._parent.logger.error(f"Failed to get details for build {build.get('id', 'unknown')}: {e}")
                    detailed_builds.append(build)
                except KeyError as e:
                    self._parent.logger.error(f"Missing key in build data: {e}")
                    detailed_builds.append(build)
                except Exception as e:
                    self._parent.logger.error(f"Unexpected error processing build {build.get('id', 'unknown')}: {e}")
                    detailed_builds.append(build)

            try:
                self._parent.stats["builds"] += len(detailed_builds)
            except Exception:
                pass
            return detailed_builds
        except ADORequestError:
            raise
        except Exception as e:
            raise ADORequestError(f"Unexpected error getting builds: {e}") from e

    async def get_code_coverage(self, project: str, build_id: int, flags: int = 7) -> Dict[str, Any]:
        try:
            session = await self._parent._get_aiohttp_session()
            url = f"{self._parent.base_url}/{project}/_apis/test/codecoverage"
            params = {"api-version": "7.1", "buildId": build_id, "flags": flags}
            data, _ = await self._parent._request_json(url, session, params, allow_not_found=True)
            return cast(Dict[str, Any], data)
        except ADORequestError:
            raise
        except aiohttp.ClientError as e:
            raise ADORequestError(f"Failed to get code coverage for build {build_id}: {e}") from e
        except Exception as e:
            raise ADORequestError(f"Unexpected error getting code coverage: {e}") from e

    async def get_build_code_coverage(self, project: str, build_id: int) -> Dict[str, Any]:
        return await self.get_code_coverage(project, build_id)

    async def get_test_results(self, project: str, build_id: int) -> Dict[str, Any]:
        try:
            session = await self._parent._get_aiohttp_session()
            url = f"{self._parent.base_url}/{project}/_apis/test/runs"
            params = {"api-version": "7.1", "buildUri": f"vstfs:///Build/Build/{build_id}", "includeRunDetails": "true"}
            test_runs = await self._parent.get_paginated_results(url, session, params)
            unlinked = sum(1 for run in test_runs if not isinstance(run, dict) or not isinstance(run.get("build"), dict) or run["build"].get("id") is None)
            test_runs = [run for run in test_runs if isinstance(run, dict) and isinstance(run.get("build"), dict) and str(run["build"].get("id")) == str(build_id)]
            if not test_runs:
                return {"results": [], "unlinked_runs": unlinked}
            total = passed = failed = skipped = not_executed = 0
            for run in test_runs:
                # REST 7.1 TestRun calls its failed count unanalyzedTests.
                # A missing passed count is not proof of zero passes.
                counts = [run.get("totalTests"), run.get("passedTests"), run.get("unanalyzedTests"), run.get("notApplicableTests", 0), run.get("incompleteTests", 0)]
                if (any(type(v) is not int or v < 0 for v in (counts[0], counts[1], counts[3], counts[4]))
                        or (counts[2] is not None and (type(counts[2]) is not int or counts[2] < 0))
                        or sum(v for v in counts[1:] if v is not None) > counts[0]):
                    return {"results": [], "unlinked_runs": unlinked, "malformed_runs": True, "linked_runs": len(test_runs)}
                total += run.get("totalTests", 0)
                passed += run.get("passedTests", 0)
                failed = failed + counts[2] if failed is not None and counts[2] is not None else None
                not_executed += run.get("notApplicableTests", 0)
                skipped += run.get("incompleteTests", 0)
            return {"results": [{"total": total, "passed": passed, "failed": failed, "skipped": skipped, "not_executed": not_executed}], "unlinked_runs": unlinked, "linked_runs": len(test_runs)}
        except ADORequestError:
            raise
        except aiohttp.ClientError as e:
            raise ADORequestError(f"Failed to get test results for build {build_id}: {e}") from e
        except Exception as e:
            raise ADORequestError(f"Unexpected error getting test results: {e}") from e

    async def get_builds_with_coverage(
        self, project: str, repository_id: Optional[str] = None, top: Optional[int] = None, **kwargs: Any
    ) -> List[Dict[str, Any]]:
        try:
            url = f"{self._parent.base_url}/{project}/_apis/build/builds"
            params: Dict[str, Any] = {
                "api-version": "7.1",
                "queryOrder": "finishTimeDescending",
                "statusFilter": "completed",
            }
            if repository_id:
                params["repositoryType"] = "TfsGit"
                params["repositoryId"] = repository_id
            if top:
                params["$top"] = top
            params.update(kwargs)

            session = await self._parent._get_aiohttp_session()
            data, _ = await self._parent._request_json(url, session, params)
            raw_builds = data.get("value", [])
            builds: List[Dict[str, Any]] = [cast(Dict[str, Any], b) for b in (raw_builds or [])]

            coverage_tasks = []
            for b in builds:
                bid = b.get("id")
                if bid is None:
                    continue
                try:
                    bid_int = int(bid)
                except Exception:
                    self._parent.logger.debug(f"Skipping non-integer build id: {bid}")
                    continue
                coverage_tasks.append((b, self.get_build_code_coverage(project, bid_int)))
            if coverage_tasks:
                coverage_results = await asyncio.gather(*(task for _, task in coverage_tasks), return_exceptions=True)
                for (build, _), coverage in zip(coverage_tasks, coverage_results):
                    if isinstance(coverage, asyncio.CancelledError):
                        raise coverage
                    if isinstance(coverage, Exception):
                        self._parent.logger.warning(f"Failed to get coverage for build {build.get('id')}: {coverage}")
                        build["coverage"] = {}
                        build.setdefault("enrichment_failures", {})["coverage"] = str(coverage)
                    else:
                        build["coverage"] = coverage
            try:
                self._parent.stats["builds"] += len(builds)
            except Exception:
                pass
            return builds
        except ADORequestError:
            raise
        except aiohttp.ClientError as e:
            raise ADORequestError(f"Failed to get builds with coverage: {e}") from e
        except Exception as e:
            raise ADORequestError(f"Unexpected error getting builds with coverage: {e}") from e

