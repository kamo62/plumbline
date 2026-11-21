"""Source isolation checks use names only; no excluded resource is contacted."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plumbline.api.models import CollectionConfig
from plumbline.backend.api.ado_client import AzureDevOpsClient, source_request_trace
from plumbline.backend.api.errors import ADORequestError
from plumbline.config.access import checked_path, validate_database_url, validate_source_url
from plumbline.config.settings import Settings


@pytest.mark.parametrize("name", ["ACME", "AcmeEngineering", "example-team", "acme-sandbox", "ACME-Engineering"])
def test_names_are_accepted_without_network(name, tmp_path):
    Settings(_env_file=None, azure_devops_pat="synthetic-pat", azure_devops_org=name)
    CollectionConfig(org=name, project=name)
    assert checked_path(tmp_path / name / "report.json") == tmp_path / name / "report.json"
    url = f"postgresql+psycopg://user:synthetic@{name}.example.com/metrics"
    assert validate_database_url(url) == url
    base = f"https://dev.azure.com/{name}"
    validate_source_url(base + "/_apis/projects", base)


@pytest.mark.asyncio
async def test_requests_are_scoped_and_redirects_rejected():
    base = "https://dev.azure.com/example-org"
    validate_source_url(base + "/project/_apis/build/builds", base)
    validate_source_url("https://vsaex.dev.azure.com/example-org/_apis/projects", base)
    validate_source_url("https://dev.azure.com/local-org/_apis/projects", "https://dev.azure.com/local-org")
    trace = source_request_trace(base)
    for url in (
        "https://dev.azure.com/ExampleEngineering/_apis/projects",
        "https://dev.azure.com/other-org/_apis/projects",
        base + "/../other-org/_apis/projects",
        base + "/%252e%252e/other-org/_apis/projects",
        "http://dev.azure.com/example-org/_apis/projects",
        "https://example.com/_apis/projects",
    ):
        with pytest.raises(ValueError):
            await trace.on_request_start[0](None, None, SimpleNamespace(url=url))
    with pytest.raises(ADORequestError, match="Redirects are not permitted"):
        await trace.on_request_redirect[0](None, None, SimpleNamespace())


def test_parent_environment_is_not_discovered(tmp_path):
    (tmp_path / ".env").write_text("DORA_PARENT_SENTINEL=must-not-load\n")
    child = tmp_path / "child"
    child.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in {"DORA_PARENT_SENTINEL", "PYTHON_DOTENV_DISABLED"}}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", "import os; import plumbline.config.settings; assert 'DORA_PARENT_SENTINEL' not in os.environ"],
        cwd=child, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr




def test_database_url_is_still_parsed():
    from sqlalchemy.exc import ArgumentError

    with pytest.raises(ArgumentError):
        validate_database_url("not a database URL")


@pytest.mark.asyncio
async def test_subclient_guard_runs_before_opening_a_connection(monkeypatch):
    import aiohttp

    connection = Mock(side_effect=AssertionError("No network connection is permitted"))
    monkeypatch.setattr(aiohttp.TCPConnector, "_create_connection", connection)
    client = AzureDevOpsClient(Settings(
        _env_file=None, azure_devops_pat="synthetic-pat", azure_devops_org="example-org",
    ))
    try:
        with pytest.raises(ADORequestError, match="configured Azure DevOps organisation"):
            await client.get_code_coverage("../other-org", 1)
        connection.assert_not_called()
    finally:
        await client.close()
