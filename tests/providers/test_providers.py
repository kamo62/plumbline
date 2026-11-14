from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from plumbline.providers import ADOProvider, EvidenceState, SCMProvider, SyntheticProvider, doctor
from plumbline.providers.synthetic import DATA, OBSERVED_AT, WINDOW


def client_fixture():
    class Client:
        async def get_project(self, p):
            return {"id": DATA[p]["id"], "name": p}
        async def get_repositories(self, p):
            return deepcopy(DATA[p]["repos"])
        async def get_builds(self, p, _unused, _start):
            return [deepcopy(DATA[p]["build"])]
        async def get_commits(self, *_args):
            return []
        async def get_branches(self, *_args):
            return []
        async def get_pull_requests(self, p, repo, start):
            assert start is None
            return deepcopy(DATA[p]["prs"][repo])
        async def get_branch_policies(self, *_args):
            return []
        async def get_code_coverage(self, p, _build):
            return deepcopy(DATA[p]["coverage"])
        async def get_test_results(self, p, _build):
            if p == "Beta":
                raise RuntimeError("private response")
            return deepcopy(DATA[p]["tests"])
        async def get_security_scan_results(self, p, _build):
            return deepcopy(DATA[p]["security"])
    return Client()


@pytest.mark.asyncio
async def test_synthetic_and_adapter_equivalent_with_scoped_builds():
    synthetic = SyntheticProvider()
    adapter = ADOProvider(client_fixture(), "synthetic", clock=lambda: OBSERVED_AT)
    assert isinstance(synthetic, SCMProvider) and isinstance(adapter, SCMProvider)
    expected = await synthetic.collect(["Alpha", "Beta"], WINDOW)
    actual = await adapter.collect(["Alpha", "Beta"], WINDOW)
    assert actual == expected
    assert len(actual.repositories) == 3
    assert sum(len(r.fields["pull_requests"].value) for r in actual.repositories) == 2
    assert actual.repositories[0].fields["pull_requests"].value[0]["reviewers"][0]["id"] == "reviewer"
    assert [(b.identity.project, b.identity.build) for b in actual.builds] == [("p1", "7"), ("p2", "7")]
    assert actual.builds[0].fields["tests"].value["results"][0]["passed"] == 8
    assert actual.builds[0].fields["tests"].state == EvidenceState.PARTIAL
    assert actual.builds[1].fields["tests"].state == EvidenceState.FAILED
    assert actual.builds[1].fields["coverage"].state == EvidenceState.OBSERVED_EMPTY


@pytest.mark.asyncio
async def test_unknown_times_and_malformed_are_visible_and_doctor_redacts():
    client = client_fixture()
    client.get_pull_requests = AsyncMock(return_value=[{"pullRequestId": 3, "extra": "retained"}])
    client.get_code_coverage = AsyncMock(return_value="bad")
    client.get_test_results = AsyncMock(return_value={"unexpected": "retained"})
    provider = ADOProvider(client, "connection", clock=lambda: OBSERVED_AT)
    snapshot = await provider.collect(["Alpha"], WINDOW)
    assert snapshot.repositories[0].fields["pull_requests"].error_code == "unknown_event_time"
    assert snapshot.repositories[0].fields["pull_requests"].value[0]["extra"] == "retained"
    assert snapshot.builds[0].fields["coverage"].error_code == "malformed_shape"
    assert snapshot.builds[0].fields["tests"].error_code == "malformed_shape"
    result = await doctor(provider, ["Alpha"], WINDOW)
    assert any(code == "malformed_shape" for _, _, code, _ in result.checks)
    assert "retained" not in repr(result) and "bad" not in repr(result)


@pytest.mark.asyncio
async def test_existing_test_client_excludes_foreign_and_exposes_unlinked():
    from plumbline.backend.api.builds_client import BuildsClient
    client = client_fixture()
    client.base_url = "https://dev.azure.com/synthetic"
    client._get_aiohttp_session = AsyncMock(return_value=object())
    client.get_paginated_results = AsyncMock(return_value=[
        {"build": {"id": 7}, "totalTests": 10, "passedTests": 8, "failedTests": 2},
        {"build": {"id": 8}, "totalTests": 99, "passedTests": 99},
        {"totalTests": 50},
    ])
    result = await BuildsClient(client).get_test_results("Alpha", 7)
    assert result["results"][0]["total"] == 10
    assert result["unlinked_runs"] == 1


@pytest.mark.asyncio
async def test_old_open_pr_zero_tests_and_late_observation_are_not_misrepresented():
    from datetime import timedelta
    client = client_fixture()
    client.get_pull_requests = AsyncMock(return_value=[{"pullRequestId": 13, "creationDate": "2025-12-01T00:00:00Z", "reviewers": []}])
    client.get_test_results = AsyncMock(return_value={"results": [{"total": 0, "passed": 0}]})
    snapshot = await ADOProvider(client, "c", clock=lambda: OBSERVED_AT + timedelta(days=1)).collect(["Alpha"], WINDOW)
    assert snapshot.repositories[0].fields["pull_requests"].value[0]["pullRequestId"] == 13
    assert snapshot.repositories[0].fields["pull_requests"].fresh_as_of is False
    assert snapshot.builds[0].fields["tests"].error_code == "test_run_presence_unknown"
    assert snapshot.builds[0].fields["security_scans"].error_code == "artifact_name_heuristic"
