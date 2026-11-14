"""Raw Data Collection Service

This module handles collecting raw data directly from Azure DevOps API endpoints
without processing or transforming the responses. Supports both single-project
and all-projects collection modes.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, TypedDict, NotRequired
import asyncio

from ...backend.api.ado_client import AzureDevOpsClient
from ...utils.logging import get_logger, log_with_context
from ...utils.time import parse_ts
# Lazy import DB modules in _get_last_run_time to avoid SQLModel redefinition issues in tests.
from ...types.data_types import (
    RepositoryData,
    create_project_info,
    merge_project_info,
)

logger = get_logger(__name__)


class BuildData(TypedDict):
    """Type definition for build data"""
    coverage: Dict[str, Any]
    tests: Dict[str, Any]
    security_scans: List[Dict[str, Any]] # Security scan results


class ConsolidatedData(TypedDict):
    """Type definition for consolidated data from all projects"""
    projects: List[Dict[str, Any]]
    repositories: List[Dict[str, Any]]
    repository_data: Dict[str, RepositoryData]
    builds: List[Dict[str, Any]]
    build_data: Dict[str, BuildData]
    project: Dict[str, Any]  # Root-level project info
    completeness: NotRequired[Dict[str, Any]]


class RawProjectData(TypedDict):
    """Type definition for raw project data"""
    project: Dict[str, Any]
    repositories: List[Dict[str, Any]]
    repository_data: Dict[str, RepositoryData]
    builds: List[Dict[str, Any]]
    build_data: Dict[str, BuildData]
    completeness: NotRequired[Dict[str, Any]]


from .base_service import MetricsService


class RawDataCollector(MetricsService):
    """Collects raw data from Azure DevOps API endpoints

    This collector supports two modes of operation:
    1. Single project collection via collect_all_raw_data()
    2. All projects collection via collect_all_projects_raw_data()

    Both modes maintain the same output data structure for consistency.
    """

    async def collect_metrics(self) -> Dict[str, Any]:
        """Collect metrics from Azure DevOps"""
        data: ConsolidatedData = await self.collect_all_projects_raw_data()
        # Convert TypedDict to dict to match base class
        return {k: v for k, v in data.items()}

    async def get_cached_metrics(self) -> Optional[Dict[str, Any]]:
        """Get metrics from cache if available"""
        return None

    def __init__(
        self,
        ado_client: AzureDevOpsClient,
        window_days: int = 180,
        minimal: bool = False,
        activity_window_days: int = 90,
        enable_delta: bool = True,
        skip_stale_repos: bool = True,
        force_year_window: bool = False,
    ):
        """Initialize the collector with an ADO client

        Args:
            ado_client: Initialized Azure DevOps client
            window_days: Number of days of historical data to collect (default: 180)
                        This window affects all metrics including builds, PRs, and commits.
                        A 180-day (6 month) window provides sufficient historical data
                        for accurate time-weighted scoring.
            minimal: If True, limit collection to 10 projects for testing purposes
            activity_window_days: Number of days to look back for determining if a repository
                                is active (default: 90). Repositories with no activity in this
                                period will be excluded from detailed data collection.
        """
        super().__init__(ado_client)
        self.window_days = window_days
        self.minimal = minimal
        self.activity_window_days = activity_window_days
        self.enable_delta = False
        self.skip_stale_repos = False
        self._policy_cache: Dict[str, Any] = {}
        self.progress_callback = None
        # Use ISO strings for logs, but datetime objects for comparisons
        now_utc = datetime.now(timezone.utc)
        # Default to N-day window unless calendar year is forced
        if force_year_window:
            jan1 = datetime(now_utc.year, 1, 1, tzinfo=timezone.utc)
            self.start_date = jan1.isoformat()
        else:
            self.start_date = (now_utc - timedelta(days=window_days)).isoformat()
        self.activity_threshold_dt = now_utc - timedelta(days=activity_window_days)
        self.activity_threshold_date = self.activity_threshold_dt.isoformat()
        # Lazily initialized DB engine for delta-mode reads
        self._db_engine = None

    async def collect_all_projects_raw_data(self, project_names: Optional[List[str]] = None) -> ConsolidatedData:
        """Collect raw data from all available projects

        Returns:
            Consolidated dictionary containing raw API responses from all projects

        Raises:
            ConnectionError: If unable to connect to Azure DevOps
            TimeoutError: If requests timeout
        """
        try:
            # Fetch all projects
            projects = await self._get_with_retry(self.ado_client.get_projects, "projects")

            if not projects and project_names is None:
                raise ValueError("No projects found in Azure DevOps")

            if project_names is not None:
                if not project_names or any(not name.strip() for name in project_names):
                    raise ValueError("Projects must contain nonempty names")
                requested = list(dict.fromkeys(name.strip() for name in project_names))
                by_name = {p["name"]: p for p in projects}
                active_projects = [by_name[name] for name in requested if name in by_name]
                missing = [name for name in requested if name not in by_name]
            else:
                active_projects = [p for p in projects if not p.get("state") or str(p["state"]).lower() == "wellformed"]
                missing = []

            # Limit to 10 projects in minimal mode
            if self.minimal and project_names is None:
                logger.info("Minimal mode enabled - limiting to 10 projects")
                active_projects = active_projects[:10]

            # Consolidated data structure for all projects
            try:
                merged_project_info = (
                    merge_project_info([create_project_info(p) for p in active_projects])
                    if isinstance(active_projects, list) and active_projects
                    else {}
                )
            except Exception:
                merged_project_info = {}

            consolidated_data: ConsolidatedData = {
                "projects": active_projects,
                "repositories": [],
                "repository_data": {},
                "builds": [],
                "build_data": {},
                "completeness": {},
                # Provide a consolidated project descriptor for clarity in downstream exports
                "project": merged_project_info,
            }
            for name in missing:
                consolidated_data["completeness"][name] = {"complete": False, "failures": [{"scope": "project", "project": name, "error": "Project not found"}]}

            # Collect data for each project
            for project in active_projects:
                project_name = project["name"]
                try:
                    log_with_context(
                        logger,
                        logging.INFO,
                        f"Starting collection for project: {project_name}",
                        project=project_name,
                    )

                    project_data = await self.collect_all_raw_data(project_name)

                    consolidated_data["completeness"][project_name] = project_data.get("completeness", {"complete": True, "failures": []})

                    # Merge repository list with project info
                    for repo in project_data.get("repositories", []):
                        repo["project"] = {**project_data.get("project", {}), **(repo.get("project") or {})}
                        consolidated_data["repositories"].append(repo)

                    # Merge repository data with project info
                    repo_projects = {repo.get("id"): repo.get("project", {}) for repo in project_data.get("repositories", [])}
                    for repo_id, repo_data in project_data.get("repository_data", {}).items():
                        repo_data["project"] = {**project_data.get("project", {}), **(repo_data.get("project") or {}), **(repo_projects.get(repo_id) or {})}
                        consolidated_data["repository_data"][repo_id] = repo_data

                    # Build enrichment is keyed only by build ID, so cross-project
                    # collisions cannot be represented safely for assessment.
                    build_data = project_data.get("build_data", {})
                    existing_ids = {str(build["id"]) for build in consolidated_data["builds"] if build.get("id") is not None}
                    incoming_ids = {str(build["id"]) for build in project_data.get("builds", []) if build.get("id") is not None}
                    collisions = (existing_ids & incoming_ids) | ({str(key) for key in consolidated_data["build_data"]} & {str(key) for key in build_data})
                    if collisions:
                        scope = consolidated_data["completeness"][project_name]
                        scope["complete"] = False
                        scope.setdefault("failures", []).append({"scope": "build", "project": project_name, "error": "Build IDs collide across projects", "build_ids": sorted(collisions)})
                    consolidated_data["build_data"].update({key: value for key, value in build_data.items() if str(key) not in collisions})
                    consolidated_data["builds"].extend(project_data.get("builds", []))

                    log_with_context(
                        logger,
                        logging.INFO,
                        f"Completed collection for project: {project_name}",
                        project=project_name,
                        repositories_count=len(project_data.get("repositories", [])),
                        builds_count=len(project_data.get("builds", [])),
                    )

                except Exception as e:
                    consolidated_data["completeness"][project_name] = {"complete": False, "failures": [{"scope": "project", "project": project_name, "error": str(e)}]}
                    log_with_context(
                        logger,
                        logging.ERROR,
                        f"Error collecting data for project {project_name}: {str(e)}",
                        project=project_name,
                        error=str(e),
                    )
                    continue

            # Log summary of collection
            log_with_context(
                logger,
                logging.INFO,
                "Completed all projects collection",
                total_projects=len(active_projects),
                total_repositories=len(consolidated_data["repositories"]),
                total_builds=len(consolidated_data["builds"]),
            )

            return consolidated_data

        except Exception as e:
            logger.error(f"Error collecting all projects data: {str(e)}")
            raise

    async def collect_all_raw_data(self, project: str) -> RawProjectData:
        """Collect all available raw data for a project

        Args:
            project: Project name or ID

        Returns:
            Dictionary containing raw API responses

        Raises:
            ValueError: If project name is invalid
            ConnectionError: If unable to connect to Azure DevOps
            TimeoutError: If requests timeout
        """
        try:
            log_with_context(
                logger,
                logging.INFO,
                f"Starting raw data collection for project: {project}",
                project=project,
            )

            # Fetch project info
            project_data = await self._get_with_retry(
                self.ado_client.get_project, "project info", project
            )

            if not project_data:
                raise ValueError(f"Project not found: {project}")

            # Transform project data structure
            try:
                transformed_project = create_project_info(project_data)
            except ValueError as e:
                logger.error(f"Error transforming project info: {str(e)}")
                raise

            # Calculate time window for project
            project_start_date = self.start_date
            # Fetch repositories
            repositories = await self._get_with_retry(
                self.ado_client.get_repositories, "repositories", project
            )

            # Exclude repositories explicitly marked disabled, not names that merely
            # resemble archival labels.
            def _looks_archived(repo: Dict[str, Any]) -> bool:
                return bool(repo.get("isDisabled", False))

            non_archived_repositories = [r for r in repositories if not _looks_archived(r)]

            if len(non_archived_repositories) < len(repositories):
                log_with_context(
                    logger,
                    logging.INFO,
                    "Excluded archived/disabled repositories",
                    project=project,
                    excluded=len(repositories) - len(non_archived_repositories),
                    remaining=len(non_archived_repositories),
                )

            active_repositories = list(non_archived_repositories)

            # Raw data dictionary for the project
            raw_data: RawProjectData = {
                "project": transformed_project,
                "repositories": active_repositories,
                "repository_data": {},
                "builds": [],
                "build_data": {},
                "completeness": {"complete": True, "failures": []},
            }

            self._policy_cache.pop(project, None)
            for repo in active_repositories:
                repo_id = repo["id"]
                repo_name = repo["name"]
                progress = {"project_id": str(transformed_project["id"]), "project_name": project,
                            "repository_id": str(repo_id), "repository_name": repo_name,
                            "status": "running", "errors": []}
                if self.progress_callback:
                    self.progress_callback(progress.copy())
                failure_start = len(raw_data["completeness"]["failures"])
                log_with_context(
                    logger,
                    logging.INFO,
                    f"Collecting data for repository: {repo_name}",
                    project=project,
                    repository=repo_name,
                )

                try:
                    repo_dict: Dict[str, Any] = {"policies": []}
                    for name, method, args in (
                        ("commits", self.ado_client.get_commits, (repo_id, project, project_start_date)),
                        ("branches", self.ado_client.get_branches, (repo_id, project)),
                        ("pull_requests", self.ado_client.get_pull_requests, (project, repo_id, project_start_date)),
                    ):
                        try:
                            repo_dict[name] = await method(*args)
                        except Exception as exc:
                            repo_dict[name] = []
                            raw_data["completeness"]["complete"] = False
                            raw_data["completeness"]["failures"].append({
                                "scope": name, "repository_id": repo_id, "error": str(exc),
                            })

                    for pr in repo_dict["pull_requests"]:
                        if pr.get("collection_failures"):
                            raw_data["completeness"]["complete"] = False
                            raw_data["completeness"]["failures"].append({
                                "scope": "pull_request", "repository_id": repo_id,
                                "pull_request_id": pr.get("pullRequestId"),
                                "errors": pr["collection_failures"],
                            })
                    # Fetch and structure repository policies
                    policies = await self._get_repository_policies(project, repo_id)
                    if policies.get("error"):
                        raw_data["completeness"]["complete"] = False
                        raw_data["completeness"]["failures"].append({"scope": "policies", "repository_id": repo_id, "error": policies["error"]})
                    repo_dict["policies"] = {
                        "repository_policies": policies.get("repository_policies", []),
                        "branch_policies": policies.get("branch_policies", []),
                        "statistics": policies.get("statistics", {
                            "total_policies": 0,
                            "blocking_policies": 0,
                            "enabled_policies": 0,
                            "policy_types": [],
                        }),
                        "scan_time": policies.get("scan_time", datetime.now(timezone.utc).isoformat())
                    }
                    
                    # Add project context
                    repo_dict["project"] = {
                        "id": raw_data["project"]["id"],
                        "name": raw_data["project"]["name"]
                    }
                    
                    # Create a type-safe RepositoryData object
                    raw_data["repository_data"][repo_id] = RepositoryData(
                        commits=repo_dict["commits"],
                        branches=repo_dict["branches"],
                        pull_requests=repo_dict["pull_requests"],
                        policies=repo_dict["policies"],
                        project=repo_dict["project"]
                    )
                except Exception as e:
                    raw_data["completeness"]["complete"] = False
                    raw_data["completeness"]["failures"].append({"scope": "repository", "repository_id": repo_id, "error": str(e)})
                    logger.error(
                        f"Error collecting repository data for {repo_name}: {str(e)}",
                        extra={"project": project, "repository": repo_name, "error": str(e)},
                    )
                if self.progress_callback:
                    failures = raw_data["completeness"]["failures"][failure_start:]
                    self.progress_callback({**progress, "status": "failed" if failures else "succeeded",
                                            "errors": failures})

            # Fetch builds
            builds = await self._get_with_retry(
                self.ado_client.get_builds, 
                "builds", 
                project,
                None,  # project_name parameter
                project_start_date
            )
            raw_data["builds"] = builds
            for build in builds:
                if build.get("enrichment_failures"):
                    raw_data["completeness"]["complete"] = False
                    raw_data["completeness"]["failures"].append({"scope": "build", "build_id": build.get("id"), "errors": build["enrichment_failures"]})

            # Collect details for each build
            for build in builds:
                build_id = build["id"]

                log_with_context(
                    logger,
                    logging.INFO,
                    f"Collecting data for build: {build_id}",
                    project=project,
                    build_id=build_id,
                )

                try:
                    outcomes = await asyncio.gather(
                        self.ado_client.get_code_coverage(project, build_id),
                        self.ado_client.get_test_results(project, build_id),
                        self.ado_client.get_security_scan_results(project, build_id),
                        return_exceptions=True,
                    )
                    failures = {}
                    defaults = [{}, {}, []]
                    for index, name in enumerate(("coverage", "tests", "security_scans")):
                        if isinstance(outcomes[index], BaseException):
                            if isinstance(outcomes[index], asyncio.CancelledError):
                                raise outcomes[index]
                            failures[name] = str(outcomes[index])
                            outcomes[index] = defaults[index]
                    coverage_data, test_data, security_scan_data = outcomes
                    if isinstance(test_data, dict) and test_data.get("unlinked_runs"):
                        failures["test_linkage"] = f"{test_data['unlinked_runs']} test runs have no build ID"
                    if failures:
                        raw_data["completeness"]["complete"] = False
                        raw_data["completeness"]["failures"].append({"scope": "build", "build_id": build_id, "errors": failures})

                    # Normalize coverage_data to the expected format
                    if coverage_data:
                        if isinstance(coverage_data, list):
                            # If it's a list, wrap it in the expected structure
                            coverage_data = {
                                "value": [
                                    {
                                        "state": "0",
                                        "modules": coverage_data
                                    }
                                ]
                            }
                        elif isinstance(coverage_data, dict):
                            if "value" not in coverage_data:
                                # If it's a dict without value, wrap it
                                coverage_data = {
                                    "value": [
                                        {
                                            "state": "0",
                                            "modules": [coverage_data] if "modules" not in coverage_data else [coverage_data]
                                        }
                                    ]
                                }
                            else:
                                # Ensure each value item has modules
                                coverage_data["value"] = [
                                    {
                                        "state": item.get("state", "0"),
                                        "modules": item.get("modules", [])
                                    }
                                    for item in coverage_data["value"]
                                ]
                    else:
                        coverage_data = {"value": [{"state": "0", "modules": []}]}

                    raw_data["build_data"][build_id] = {
                        "coverage": coverage_data,
                        "tests": (
                            test_data
                            if test_data
                            else {"results": [{"total": 0, "passed": 0, "failed": 0}]}
                        ),
                        "security_scans": security_scan_data if security_scan_data else [],
                        "failures": failures,
                    }
                except Exception as e:
                    logger.error(
                        f"Error collecting build data for build {build_id}: {str(e)}",
                        extra={"project": project, "build_id": build_id, "error": str(e)},
                    )
                    raw_data["build_data"][build_id] = {
                        "coverage": {"value": []},
                        "tests": {"results": [{"total": 0, "passed": 0, "failed": 0}]},
                        "security_scans": []
                    }

            log_with_context(
                logger,
                logging.INFO,
                f"Completed raw data collection for project: {project}",
                project=project,
                repositories_count=len(active_repositories),
                builds_count=len(builds),
            )
            return raw_data

        except ValueError as e:
            logger.error(f"Invalid project: {str(e)}")
            raise
        except ConnectionError as e:
            logger.error(f"Connection error: {str(e)}")
            raise
        except TimeoutError as e:
            logger.error(f"Request timeout: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error collecting raw data: {str(e)}")
            raise

    async def _get_with_retry(self, func: Any, operation: str, *args: Any) -> Any:
        """Execute API call with retry logic

        Args:
            func: API function to call
            operation: Operation name for logging
            *args: Arguments for the function

        Returns:
            API response data

        Raises:
            TimeoutError: If all retries fail
        """
        # HTTP transport owns retries; preserve the original error and cancellation.
        return await func(*args)

    def _get_last_run_time(self, project: str) -> Optional[datetime]:
        """Return most recent run time for project from DB, or None.

        Uses env var PLUMBLINE_DATABASE_URL. Does not create tables.
        """
        db_url = os.getenv("PLUMBLINE_DATABASE_URL")
        if not db_url:
            return None
        try:
            from importlib import import_module
            models = import_module("plumbline.persistence.models")
            init_engine_and_tables = getattr(models, "init_engine_and_tables")
            Run = getattr(models, "Run")
            from sqlmodel import Session, select  # type: ignore

            if self._db_engine is None:
                self._db_engine = init_engine_and_tables(db_url, allow_create_all=False)
            org = getattr(getattr(self.ado_client, "settings", object()), "azure_devops_org", None)
            with Session(self._db_engine) as session:
                stmt = select(Run).order_by(Run.executed_at.desc())
                if org:
                    stmt = stmt.where(Run.org == org)
                if project:
                    stmt = stmt.where(Run.project == project)
                run = session.exec(stmt).first()
                return run.executed_at if run else None
        except Exception as e:
            logger.debug(f"_get_last_run_time error: {e}")
            return None

    async def _get_repository_policies(self, project: str, repo_id: str) -> Dict[str, Any]:
        """Get all policies for a repository

        Args:
            project: Project name or ID
            repo_id: Repository ID

        Returns:
            Dictionary containing all policies and their statistics
        """
        try:
            if project not in self._policy_cache:
                configs = await self._get_with_retry(self.ado_client.get_policy_configurations, "policy configurations", project)
                types = await self._get_with_retry(self.ado_client.get_policy_types, "policy types", project)
                self._policy_cache[project] = (configs, types)
            policy_configs, policy_types = self._policy_cache[project]

            # Create a lookup map for policy types
            policy_type_map = {pt["id"]: pt for pt in policy_types}

            repository_policies: List[Dict[str, Any]] = []
            branch_policies: List[Dict[str, Any]] = []
            all_policies_for_repo: List[Dict[str, Any]] = []

            for policy in policy_configs:
                policy_scope = policy.get("settings", {}).get("scope", [])
                
                # Ensure policy_scope is a list for consistent processing
                if not isinstance(policy_scope, list):
                    policy_scope = [policy_scope] if policy_scope else []
                
                # Policy applies if its scope matches the repo or is project-level (repo ID is null).
                applies_to_repo = False
                has_specific_repo_id = False
                is_project_level = False
                
                for scope_item in policy_scope:
                    if not isinstance(scope_item, dict):
                        continue
                    
                    repo_id_value = scope_item.get("repositoryId")
                    
                    # Check if this is a project-level policy 
                    # (repositoryId is None/null OR missing entirely)
                    if ("repositoryId" in scope_item and repo_id_value is None) or "repositoryId" not in scope_item:
                        is_project_level = True
                        applies_to_repo = True
                        logger.debug(
                            f"Found project-level policy {policy.get('id')} with null/missing repositoryId"
                        )
                    # Check if this is a repo-specific policy
                    elif repo_id_value == repo_id:
                        has_specific_repo_id = True
                        applies_to_repo = True
                        break
                
                if not applies_to_repo:
                    continue # Skip policies not related to this repository

                # Add policy type display name
                policy_type_id = policy.get("type", {}).get("id")
                policy_type = policy_type_map.get(policy_type_id)
                if policy_type:
                    policy["type"] = policy_type

                all_policies_for_repo.append(policy)

                # Categorize into repository or branch policies
                has_branch_scope = False
                for scope_item in policy_scope:
                    if isinstance(scope_item, dict) and scope_item.get("refName"):
                        has_branch_scope = True
                        break
                        
                if has_branch_scope:
                    # This is a branch-specific policy
                    # Extract branch name if available
                    branch_name = "Unknown"
                    is_default_branch = False
                    
                    for scope_item in policy_scope:
                        if isinstance(scope_item, dict) and scope_item.get("refName"):
                            ref_name = scope_item.get("refName", "")
                            branch_name = ref_name.replace("refs/heads/", "") if ref_name else "Unknown"
                            # Check if it's the default branch (assuming 'main' or 'master' for simplicity)
                            if branch_name in ["main", "master"]: # This might need to be dynamic based on actual default branch
                                is_default_branch = True
                            break
                    
                    # Find or create the branch entry in branch_policies
                    branch_entry = next(
                        (item for item in branch_policies if item["branch_name"] == branch_name and 
                         item.get("is_project_level", False) == is_project_level),
                        None
                    )
                    if not branch_entry:
                        branch_entry = {
                            "branch_name": branch_name,
                            "is_default_branch": is_default_branch,
                            "policies": [],
                            "is_project_level": is_project_level
                        }
                        branch_policies.append(branch_entry)
                    branch_entry["policies"].append(policy)
                else:
                    # This is a repository-wide policy
                    repository_policies.append(policy)

            # Calculate statistics based on all policies found for the repository
            policy_count = len(all_policies_for_repo)
            blocking_policies = sum(1 for p in all_policies_for_repo if p.get("isBlocking", False))
            enabled_policies = sum(1 for p in all_policies_for_repo if p.get("isEnabled", False))
            policy_types_used = list(
                set(p.get("type", {}).get("displayName", "Unknown") for p in all_policies_for_repo)
            )
            
            # Count project-level vs repository-level policies
            project_level_count = sum(1 for p in all_policies_for_repo 
                                    if any(isinstance(scope_item, dict) and 
                                          (("repositoryId" in scope_item and scope_item.get("repositoryId") is None) or
                                           "repositoryId" not in scope_item)
                                          for scope_item in p.get("settings", {}).get("scope", [])))
            repo_level_count = policy_count - project_level_count
            
            # Log policy counts for debugging
            logger.info(
                f"Found policies for repository {repo_id} in project {project}: "
                f"total={policy_count}, repo={len(repository_policies)}, "
                f"branch={sum(len(b['policies']) for b in branch_policies)}, "
                f"project_level={project_level_count}, repo_level={repo_level_count}"
            )

            return {
                "repository_policies": repository_policies,
                "branch_policies": branch_policies,
                "statistics": {
                    "total_policies": policy_count,
                    "blocking_policies": blocking_policies,
                    "enabled_policies": enabled_policies,
                    "policy_types": policy_types_used,
                    "project_level_policies": project_level_count,
                    "repository_level_policies": repo_level_count,
                },
                "scan_time": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.error(
                f"Error getting policies for repository {repo_id} in project {project}: {str(e)}",
                extra={"project": project, "repository_id": repo_id, "error": str(e)},
            )
            return {
                "repository_policies": [],
                "branch_policies": [],
                "statistics": {
                    "total_policies": 0,
                    "blocking_policies": 0,
                    "enabled_policies": 0,
                    "policy_types": [],
                    "project_level_policies": 0,
                    "repository_level_policies": 0,
                },
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            }
