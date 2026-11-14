from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from plumbline.backend.api.ado_client import AzureDevOpsClient
from plumbline.backend.api.errors import ADORequestError
from plumbline.backend.services.raw_data_collector import RawDataCollector


@pytest.mark.asyncio
async def test_rate_limit_is_bounded_and_keeps_cause(monkeypatch):
    client = object.__new__(AzureDevOpsClient)
    client.base_url = 'https://dev.azure.com/test-org'
    response = MagicMock()
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    response.raise_for_status.side_effect = aiohttp.ClientResponseError(
        MagicMock(real_url='https://dev.azure.com/test-org/a'), (), status=429,
        message='rate limited', headers={'Retry-After': '0'},
    )
    session = MagicMock()
    session.get.return_value = response
    with pytest.raises(ADORequestError) as error:
        await client._request_json('https://dev.azure.com/test-org/a', session)
    assert session.get.call_count == 3
    assert error.value.__cause__.status == 429


@pytest.mark.asyncio
async def test_test_runs_only_count_explicit_matching_build():
    client = object.__new__(AzureDevOpsClient)
    client.base_url = 'https://dev.azure.com/test-org'
    client._get_aiohttp_session = AsyncMock(return_value=object())
    client.get_paginated_results = AsyncMock(return_value=[
        {'build': {'id': 42}, 'totalTests': 5, 'passedTests': 4, 'failedTests': 1},
        {'build': {'id': 43}, 'totalTests': 10, 'passedTests': 10},
        {'totalTests': 20, 'passedTests': 20},
    ])
    from plumbline.backend.api.builds_client import BuildsClient
    result = await BuildsClient(client).get_test_results('project', 42)
    assert result['results'][0]['total'] == 5
    assert result['results'][0]['passed'] == 4
    assert result['unlinked_runs'] == 1
    assert client.get_paginated_results.await_args.args[2]['buildUri'] == 'vstfs:///Build/Build/42'


@pytest.mark.asyncio
async def test_real_collector_preserves_successes_and_reports_failure():
    client = MagicMock()
    client.get_project = AsyncMock(return_value={'id': 'p', 'name': 'project'})
    client.get_repositories = AsyncMock(return_value=[{'id': 'r', 'name': 'repo', 'lastUpdateTime': '2000-01-01T00:00:00Z'}])
    client.get_commits = AsyncMock(return_value=[{'commitId': 'c'}])
    client.get_branches = AsyncMock(side_effect=RuntimeError('branches unavailable'))
    client.get_pull_requests = AsyncMock(return_value=[])
    client.get_policy_configurations = AsyncMock(return_value=[])
    client.get_policy_types = AsyncMock(return_value=[])
    client.get_builds = AsyncMock(return_value=[{'id': 42}])
    client.get_code_coverage = AsyncMock(return_value={'value': [{'modules': [{'name': 'synthetic'}]}]})
    client.get_test_results = AsyncMock(side_effect=RuntimeError('tests unavailable'))
    client.get_security_scan_results = AsyncMock(return_value=[{'name': 'security'}])
    collector = RawDataCollector(client, enable_delta=True, skip_stale_repos=True)
    collector._get_last_run_time = MagicMock(side_effect=AssertionError('must not read watermark'))
    result = await collector.collect_all_raw_data('project')
    assert result['repository_data']['r']['commits'] == [{'commitId': 'c'}]
    assert client.get_commits.await_args.args[2] == collector.start_date
    build = result['build_data'][42]
    assert build['coverage']['value'][0]['modules'][0]['name'] == 'synthetic'
    assert build['security_scans'] == [{'name': 'security'}]
    assert build['failures'] == {'tests': 'tests unavailable'}
    assert result['completeness']['complete'] is False
    assert any(f['scope'] == 'branches' for f in result['completeness']['failures'])


@pytest.mark.asyncio
async def test_commit_paging_uses_commit_filters():
    client = object.__new__(AzureDevOpsClient)
    client.base_url = 'https://dev.azure.com/test-org'
    client._get_aiohttp_session = AsyncMock(return_value=object())
    calls = []
    async def page(url, session, params):
        calls.append(dict(params))
        return [{'commitId': str(i)} for i in range(1000)] if params['$skip'] == 0 else [{'commitId': 'last'}]
    client.get_paginated_results = page
    commits = await client.get_commits('repo', 'project', '2026-01-01', '2026-02-01')
    assert len(commits) == 1001
    assert calls[1]['$skip'] == 1000
    assert calls[0]['searchCriteria.fromDate'] == '2026-01-01'
    assert calls[0]['searchCriteria.toDate'] == '2026-02-01'
