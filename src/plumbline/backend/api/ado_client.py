"""
Azure DevOps API Client Module
"""

import requests # type: ignore
import base64
import pandas as pd
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time
from typing import Dict, List, Any, Optional, cast
from ...utils.logging import get_logger
from ratelimit import limits, sleep_and_retry # type: ignore
from requests.adapters import HTTPAdapter # type: ignore
from requests.packages.urllib3.util.retry import Retry # type: ignore
from tqdm import tqdm # type: ignore
from pathlib import Path
import asyncio
import aiohttp
from .errors import ADORequestError, ADOPermanentError, ADOTransientError
from .builds_client import BuildsClient
from .policies_client import PoliciesClient
from ...config.access import validate_org, validate_source_url, validate_target

# Setup logging via central logger utility
logger = get_logger(__name__)

# Disable tqdm output to stderr to prevent mixing with logs
tqdm.monitor_interval = 0

# Rate limiting configuration
CALLS_PER_SECOND = 5
SECONDS_PER_CALL = 1 / CALLS_PER_SECOND
_REQUEST_BUDGET_SECONDS = 60.0


def _retry_delay(value: Any, fallback: float) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return max(0.0, delay) if math.isfinite(delay) else fallback


def initialize_headers(settings: Any) -> Dict[str, str]:
    """Initialize HTTP headers for Azure DevOps API calls.

    Only the explicitly configured PAT is used. Ambient pipeline tokens are ignored.
    """
    # Explicit PAT only
    raw_pat = settings.azure_devops_pat.get_secret_value()
    pat_token = (raw_pat or "").strip().strip('"').strip("'")
    if raw_pat != pat_token and raw_pat:
        logger.debug("PAT contained surrounding quotes or whitespace; sanitized before use")
    if not pat_token:
        raise ADORequestError("Set PLUMBLINE_AZURE_DEVOPS_PAT explicitly before collecting data.")
    encoded_pat = base64.b64encode(f":{pat_token}".encode()).decode()
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Basic {encoded_pat}",
    }


def setup_http_session() -> requests.Session:
    """Configure HTTP session with retry logic"""
    session = requests.Session()
    session.trust_env = False
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@sleep_and_retry # type: ignore
@limits(calls=CALLS_PER_SECOND, period=SECONDS_PER_CALL) # type: ignore
def make_request(url: str, session: requests.Session) -> Any:
    """Make a rate-limited request to the Azure DevOps API"""
    validate_target(url)
    try:
        logger.debug(f"Making request to: {url}")
        response = session.get(url, allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise ADORequestError("Redirects are not permitted")
        response.raise_for_status()
        data = response.json()
        logger.debug(f"Response status: {response.status_code}")
        return data
    except requests.exceptions.RequestException as e:
        error_msg = f"Error accessing {url}: {str(e)}"
        if response := getattr(e, "response", None):
            if response.status_code == 404:
                # Debug level for 404s as they're often expected
                logger.debug(error_msg)
            else:
                # Warning level for other errors
                logger.warning(error_msg)
        raise ADORequestError(error_msg)


def source_request_trace(base_url: str) -> aiohttp.TraceConfig:
    """Validate every real async request, including those made by subclients."""
    trace = aiohttp.TraceConfig()

    async def check_request(session: Any, context: Any, params: Any) -> None:
        validate_source_url(str(params.url), base_url)

    async def reject_redirect(session: Any, context: Any, params: Any) -> None:
        raise ADORequestError("Redirects are not permitted")

    trace.on_request_start.append(check_request)
    trace.on_request_redirect.append(reject_redirect)
    return trace


class AzureDevOpsClient:
    """Azure DevOps API client wrapper"""

    def __init__(self, settings: Any):
        """Initialize the Azure DevOps client with settings"""
        self.settings = settings
        self.base_url = f"https://dev.azure.com/{validate_org(settings.azure_devops_org)}"
        logger.debug(f"Initializing ADO client with base URL: {self.base_url}")
        self.session = setup_http_session()
        self.headers = initialize_headers(settings)
        # Debug (safe): show whether a PAT is configured without revealing it
        try:
            token_len = len(settings.azure_devops_pat.get_secret_value() or "")
        except Exception:
            token_len = 0
        # Don’t log bearer presence explicitly, just PAT metadata (length only)
        logger.debug(f"ADO org: {settings.azure_devops_org}; PAT configured: {bool(token_len)}; PAT length: {token_len}")
        self.session.headers.update(self.headers)
        # Async HTTP settings: timeout, connector, and concurrency guard
        try:
            timeout_total = float(getattr(self.settings, "http_timeout_total", 60))
        except Exception:
            timeout_total = 60.0
        self._timeout = aiohttp.ClientTimeout(total=timeout_total)
        self._connector_limit = int(getattr(self.settings, "concurrency_expensive", 10) or 10)
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._semaphore_expensive: Optional[asyncio.Semaphore] = asyncio.Semaphore(max(1, self._connector_limit))
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        # Lightweight runtime counters for progress heartbeats
        self.stats: Dict[str, int] = {
            "pages": 0,
            "prs": 0,
            "builds": 0,
            "repos": 0,
            "projects": 0,
        }
        # Expose logger on instance for subclients
        self.logger = logger
        # Subclients
        self.builds = BuildsClient(self)
        self.policies = PoliciesClient(self)

    async def __aenter__(self) -> "AzureDevOpsClient":
        """Async context manager entry"""
        await self._get_aiohttp_session()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit"""
        await self.close()

    async def close(self) -> None:
        """Close the aiohttp session"""
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    async def _get_aiohttp_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session"""
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            if self._connector is None:
                self._connector = aiohttp.TCPConnector(limit=max(1, self._connector_limit))
            self._aiohttp_session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=self._timeout,
                connector=self._connector,
                trace_configs=[source_request_trace(self.base_url)],
                trust_env=False,
            )
        return self._aiohttp_session

    async def _request_json(self, url: str, session: Any, params: Optional[Dict[str, Any]] = None, *, allow_not_found: bool = False) -> Any:
        """Request one page with bounded retries for transient transport failures."""
        validate_source_url(url, self.base_url)
        deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
        for attempt in range(3):
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    async with session.get(url, params=params) as response:
                        if allow_not_found and response.status == 404:
                            return {}, response.headers
                        response.raise_for_status()
                        data = await response.json()
                        if not isinstance(data, dict):
                            raise ADOPermanentError(f"Invalid JSON payload from {url}", status=response.status)
                        return data, response.headers
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                raise ADOPermanentError(f"Invalid JSON payload from {url}: {exc}", status=getattr(exc, "status", None)) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                status = getattr(exc, "status", None)
                if isinstance(exc, aiohttp.ClientResponseError):
                    transient = status in (429, 500, 502, 503, 504)
                else:
                    transient = not isinstance(exc, (aiohttp.InvalidURL, aiohttp.ClientSSLError))
                error_type = ADOTransientError if transient else ADOPermanentError
                remaining = deadline - time.monotonic()
                if not transient or attempt == 2 or remaining <= 0:
                    raise error_type(f"API request failed for {url}: {exc}", status=status) from exc
                headers = getattr(exc, "headers", None) or {}
                delay = min(_retry_delay(headers.get("Retry-After"), 2 ** attempt), remaining)
                await asyncio.sleep(delay)
                if time.monotonic() >= deadline:
                    raise ADOTransientError(f"API request budget exhausted for {url}: {exc}", status=status) from exc
        raise AssertionError("unreachable")

    async def get_paginated_results(
        self, url: str, session: Optional[aiohttp.ClientSession] = None, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        session = session or await self._get_aiohttp_session()
        params = dict(params or {})
        results: List[Dict[str, Any]] = []
        while True:
            data, headers = await self._request_json(url, session, params)
            results.extend(data["value"] if "value" in data else [data])
            self.stats["pages"] += 1
            token = headers.get("x-ms-continuationtoken") or headers.get("continuationToken")
            if not token:
                return results
            params["continuationToken"] = token

    async def get_projects(self) -> List[Dict[str, Any]]:
        """Get all projects

        Returns:
            List of project dictionaries

        Raises:
            ADORequestError: If API request fails
        """
        url = f"{self.base_url}/_apis/projects"
        params = {"api-version": "7.1"}

        try:
            session = await self._get_aiohttp_session()
            projs = await self.get_paginated_results(url, session, params)
            try:
                self.stats["projects"] = len(projs)
            except Exception:
                pass
            return projs
        except ADORequestError:
            raise
        except Exception as e:
            error_msg = f"Failed to get projects: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    async def get_security_scan_results(self, project: str, build_id: int) -> List[Dict[str, Any]]:
        return await self.builds.get_security_scan_results(project, build_id)

    async def get_project(self, project_name: str) -> Dict[str, Any]:
        """Get a single project by name

        Args:
            project_name: Project name or ID

        Returns:
            Dictionary containing project information

        Raises:
            ADORequestError: If API request fails
        """
        try:
            session = await self._get_aiohttp_session()

            url = f"{self.base_url}/_apis/projects/{project_name}"
            params = {"api-version": "7.1"}

            data, _ = await self._request_json(url, session, params)
            return cast(Dict[str, Any], data)

        except ADORequestError:
            raise
        except aiohttp.ClientError as e:
            error_msg = f"Failed to get project {project_name}: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error getting project: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    async def get_repositories(self, project: str) -> List[Dict[str, Any]]:
        """Get all repositories in a project

        Args:
            project: Project name or ID

        Returns:
            List of repository dictionaries

        Raises:
            ADORequestError: If API request fails
        """
        url = f"{self.base_url}/{project}/_apis/git/repositories"
        params = {
            "api-version": "7.1",
            "includeAllUrls": "true",  # Include all URLs
            "includeLinks": "true",    # Include reference links 
            "includeHidden": "true"    # Include hidden repositories
        }

        try:
            logger.debug(f"Getting repositories for project: {project}")
            session = await self._get_aiohttp_session()
            repos = await self.get_paginated_results(url, session, params)
            
            # Log a sample repository to examine its fields
            if repos and len(repos) > 0:
                logger.debug(f"Repository example fields: {list(repos[0].keys())}")
            try:
                self.stats["repos"] += len(repos)
            except Exception:
                pass
            return repos
        except ADORequestError:
            raise
        except Exception as e:
            error_msg = f"Failed to get repositories for project {project}: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    async def get_commits(
        self, repository_id: str, project_name: str, from_date: Optional[str] = None, to_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get commits for a repository

        Args:
            repository_id: Repository ID
            project_name: Project name or ID
            from_date: Optional start date for commit history
            to_date: Optional end date for commit history

        Returns:
            List of commit dictionaries

        Raises:
            ADORequestError: If API request fails
        """
        url = f"{self.base_url}/{project_name}/_apis/git/repositories/{repository_id}/commits"
        params = {"api-version": "7.1"}

        if from_date:
            params["searchCriteria.fromDate"] = from_date
        if to_date:
            params["searchCriteria.toDate"] = to_date

        try:
            session = await self._get_aiohttp_session()
            results: List[Dict[str, Any]] = []
            params["$top"] = 1000
            params["$skip"] = 0
            while True:
                page = await self.get_paginated_results(url, session, params)
                results.extend(page)
                if len(page) < params["$top"]:
                    return results
                params["$skip"] += len(page)
        except ADORequestError:
            raise
        except Exception as e:
            error_msg = f"Failed to get commits for repository {repository_id}: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    async def get_branches(self, repository_id: str, project_name: str) -> List[Dict[str, Any]]:
        """Get branches for a repository

        Args:
            repository_id: Repository ID
            project_name: Project name or ID

        Returns:
            List of branch dictionaries

        Raises:
            ADORequestError: If API request fails
        """
        url = f"{self.base_url}/{project_name}/_apis/git/repositories/{repository_id}/refs"
        params = {"api-version": "7.1"}

        try:
            session = await self._get_aiohttp_session()
            return await self.get_paginated_results(url, session, params)
        except ADORequestError:
            raise
        except Exception as e:
            error_msg = f"Failed to get branches for repository {repository_id}: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    async def get_branch_policies(self, project_name: str, repository_id: str) -> List[Dict[str, Any]]:
        """Get branch policies for a repository

        Args:
            project_name: Project name or ID
            repository_id: Repository ID

        Returns:
            List of policy configuration dictionaries

        Raises:
            ADORequestError: If API request fails
        """
        url = f"{self.base_url}/{project_name}/_apis/policy/configurations"
        params = {"api-version": "7.1", "repositoryId": repository_id}

        try:
            session = await self._get_aiohttp_session()
            return await self.get_paginated_results(url, session, params)
        except ADORequestError:
            raise
        except Exception as e:
            error_msg = f"Failed to get branch policies for repository {repository_id}: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    def _get_repository_id(self, repo: Any) -> str:
        """Extract repository ID from repository object"""
        if isinstance(repo, dict):
            rid = repo.get("id")
            if rid is None:
                raise ValueError("Repository object missing 'id'")
            return str(rid)
        return str(repo)

    async def get_pull_requests(
        self, project: str, repository_id: str, min_time: Optional[str] = None, *, candidate=None
    ) -> List[Dict[str, Any]]:
        """Get pull requests for a repository

        Args:
            project: Project name or ID
            repository_id: Repository ID
            min_time: Optional ISO format date to filter pull requests from

        Returns:
            List of pull request dictionaries with detailed information

        Raises:
            ADORequestError: If API request fails
        """
        try:
            session = await self._get_aiohttp_session()

            # Get base pull request list
            url = f"{self.base_url}/{project}/_apis/git/repositories/{repository_id}/pullrequests"
            params: Dict[str, Any] = { # Explicitly type params
                "api-version": "7.1",
                "searchCriteria.status": "all",
                "searchCriteria.includeLinks": "true",
                "$top": 1000,
                "$skip": 0,
                "$orderby": "creationDate desc"
            }
            
            # Add time-based filtering if min_time is provided
            if min_time:
                params["searchCriteria.minTime"] = min_time
                params["searchCriteria.queryTimeRangeType"] = "created"
                logger.info(f"Filtering pull requests created after {min_time}")

            logger.info(f"Fetching pull requests with params: {params}")
            
            # Get all pages of results
            all_prs: List[Dict[str, Any]] = []
            while True:
                data, _ = await self._request_json(url, session, params)
                page_results = data.get("value", [])
                all_prs.extend(page_results)
                if len(page_results) < 1000:
                    break
                params["$skip"] = int(params["$skip"]) + len(page_results)

            logger.info(f"Found {len(all_prs)} total pull requests")
            try:
                self.stats["prs"] += len(all_prs)
            except Exception:
                pass

            if candidate is not None:
                all_prs = [pr for pr in all_prs if candidate(pr)]

            # Get details for each PR with limited concurrency
            detailed_prs: List[Dict[str, Any]] = []

            async def _fetch_pr_details(pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                async with self._semaphore_expensive:
                    try:
                        if not isinstance(pr, dict):
                            return {"collection_failures": {"details": "Malformed pull request"}}
                        pr_id = pr.get("pullRequestId")
                        if not pr_id:
                            logger.warning(f"PR missing pullRequestId: {pr.get('title', 'unknown')} in {project}/{repository_id}")
                            return {**pr, "collection_failures": {"details": "Missing pullRequestId"}}
                        logger.debug(f"Processing PR {pr_id} for {project}/{repository_id}")

                        # Get PR details including reviewers
                        details_url = f"{self.base_url}/{project}/_apis/git/repositories/{repository_id}/pullrequests/{pr_id}"
                        details_params = {
                            "api-version": "7.1",
                            "includeWorkItemRefs": "true",
                            "$expand": "reviewers,commits,changes"
                        }

                        pr_details, _ = await self._request_json(details_url, session, details_params)

                        # Get PR commits
                        commits_url = f"{details_url}/commits"
                        try:
                            pr_details["commits"] = await self.get_paginated_results(
                                commits_url, session, {"api-version": "7.1"}
                            )
                        except ADORequestError as exc:
                            pr_details.setdefault("collection_failures", {})["commits"] = str(exc)
                            pr_details["commits"] = []
                            logger.debug(f"No commits found for PR {pr_id}")

                        # Get PR threads (comments and code review feedback)
                        threads_url = f"{details_url}/threads"
                        try:
                            threads = await self.get_paginated_results(
                                threads_url, session, {"api-version": "7.1"}
                            )
                            pr_details["threads"] = threads

                            # Update reviewer vote dates from thread comments
                            reviewers = pr_details.get("reviewers", [])
                            for thread in threads:
                                for comment in thread.get("comments", []):
                                    if comment.get("commentType") == "system" and "voted" in str(comment.get("content", "")).lower():
                                        for reviewer in reviewers:
                                            if reviewer.get("id") and reviewer["id"] in str(comment.get("content", "")):
                                                reviewer["voteDate"] = comment.get("publishedDate")
                                                break
                        except ADORequestError as exc:
                            pr_details.setdefault("collection_failures", {})["threads"] = str(exc)
                            pr_details["threads"] = []
                            logger.debug(f"No threads found for PR {pr_id}")
                        return pr_details
                    except Exception as e:
                        logger.error(
                            f"Unexpected error processing PR {pr.get('pullRequestId', 'unknown')}: {e}"
                        )
                        return {**pr, "collection_failures": {"details": str(e)}}

            if self._semaphore_expensive is None:
                self._semaphore_expensive = asyncio.Semaphore(max(1, self._connector_limit))
            tasks = [_fetch_pr_details(pr) for pr in all_prs]
            results = await asyncio.gather(*tasks, return_exceptions=False)
            for res in results:
                if isinstance(res, dict):
                    detailed_prs.append(res)
            return detailed_prs

        except ADORequestError as e:
            error_msg = f"Failed to get pull requests for {project}/{repository_id}: {e}"
            logger.error(error_msg)
            raise
        except Exception as e:
            error_msg = f"Unexpected error getting pull requests: {str(e)}"
            logger.error(error_msg)
            raise ADORequestError(error_msg) from e

    async def get_pull_requests_window(self, project, repository_id, start, end):
        from plumbline.providers.ado import _time
        def candidate(pr):
            if not isinstance(pr, dict):
                return True
            created = _time(pr.get("creationDate"))
            closed = _time(pr.get("closedDate"))
            if created is None or (pr.get("closedDate") is not None and closed is None):
                return True
            return created < end and (closed is None or closed >= start)
        return await self.get_pull_requests(project, repository_id, None, candidate=candidate)

    async def get_builds_window(self, project, start, end, observed_at):
        from datetime import timedelta
        from plumbline.providers.ado import _time

        def candidate(build):
            if not isinstance(build, dict):
                return True
            finish = _time(build.get("finishTime"))
            if finish is not None:
                return start <= finish < end
            if build.get("finishTime") is None and observed_at >= end and str(build.get("status", "")).lower() in {"inprogress", "notstarted", "postponed", "cancelling"}:
                return False
            return True

        return await self.builds.get_builds(project, from_date=(start - timedelta(microseconds=1)).isoformat(), candidate=candidate)

    async def get_builds(self, project: str, project_name: Optional[str] = None, from_date: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self.builds.get_builds(project, project_name, from_date)

    async def get_code_coverage(self, project: str, build_id: int, flags: int = 7) -> Dict[str, Any]:
        return await self.builds.get_code_coverage(project, build_id, flags)

    async def process_data_to_excel(self, excel_data: Dict[str, Any], all_data: Dict[str, Any], timestamp: str) -> None:
        """Process data and export it to Excel and JSON files"""
        # Create exports directory with date subdirectory
        exports_dir = Path("exports") / datetime.now().strftime("%Y-%m-%d")
        exports_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Exporting data to files...")

        # Export to Excel
        try:
            excel_filename = exports_dir / f"ADO_Detailed_Report_{timestamp}.xlsx"
            with pd.ExcelWriter(excel_filename) as writer:
                for sheet_name, data in excel_data.items():
                    if data:  # Only create sheet if we have data
                        logger.info(f"Writing {sheet_name} sheet with {len(data)} rows")
                        df = pd.DataFrame(data)
                        df.to_excel(writer, sheet_name=sheet_name, index=False)
            logger.info(f"Data exported to {excel_filename}")
        except Exception as e:
            logger.error(f"Failed to export Excel file: {e}")

        # Export raw data to separate JSON files
        try:
            for data_type, data in all_data.items():
                if data:  # Only create file if we have data
                    json_filename = exports_dir / f"ADO_{data_type}_{timestamp}.json"
                    logger.info(f"Writing {data_type} JSON with {len(data)} items")
                    with open(json_filename, "w") as f:
                        json.dump(data, f, indent=2, default=str)
                    logger.info(f"{data_type.capitalize()} data exported to {json_filename}")
        except Exception as e:
            logger.error(f"Failed to export JSON files: {e}")

    async def get_test_results(self, project: str, build_id: int) -> Dict[str, Any]:
        return await self.builds.get_test_results(project, build_id)

    async def get_build_code_coverage(self, project: str, build_id: int) -> Dict[str, Any]:
        try:
            # Preserve behavior: delegate through self.get_code_coverage so monkeypatching works in tests
            return await self.get_code_coverage(project, build_id)
        except ADORequestError:
            raise
        except Exception as e:
            raise ADORequestError(f"Unexpected error getting build code coverage: {e}") from e

    async def get_builds_with_coverage(self, project: str, repository_id: Optional[str] = None, top: Optional[int] = None, **kwargs: Any) -> List[Dict[str, Any]]:
        return await self.builds.get_builds_with_coverage(project, repository_id, top, **kwargs)

    async def get(self, url: str) -> Dict[str, Any]:
        """Make a GET request to the Azure DevOps API"""
        validate_source_url(url, self.base_url)
        session = await self._get_aiohttp_session()
        data, _ = await self._request_json(url, session, allow_not_found=True)
        return cast(Dict[str, Any], data or {"value": []})

    async def get_policy_configurations(self, project: str) -> List[Dict[str, Any]]:
        return await self.policies.get_policy_configurations(project)

    async def get_policy_types(self, project: str) -> List[Dict[str, Any]]:
        return await self.policies.get_policy_types(project)
