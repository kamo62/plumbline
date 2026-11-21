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
        async def get_policy_configurations(self, *_args):
            return []
        async def get_policy_types(self, *_args):
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
        {"build": {"id": 7}, "totalTests": 10, "passedTests": 8, "unanalyzedTests": 2},
        {"build": {"id": 8}, "totalTests": 99, "passedTests": 99},
        {"totalTests": 50},
    ])
    result = await BuildsClient(client).get_test_results("Alpha", 7)
    assert result["results"][0]["total"] == 10
    assert result["results"][0]["failed"] == 2
    assert result["unlinked_runs"] == 1


@pytest.mark.asyncio
async def test_malformed_test_counts_export_as_unavailable(tmp_path):
    import pandas as pd
    from plumbline.backend.api.builds_client import BuildsClient
    from plumbline.backend.services.raw_data_exporter import RawDataExporter
    client = client_fixture()
    client.base_url = 'https://dev.azure.com/synthetic'
    client._get_aiohttp_session = AsyncMock(return_value=object())
    client.get_paginated_results = AsyncMock(return_value=[
        {'build': {'id': 7}, 'totalTests': 10, 'passedTests': 11}])
    tests = await BuildsClient(client).get_test_results('Alpha', 7)
    assert tests['malformed_runs']
    raw = {'project': {'name': 'Alpha'}, 'repositories': [], 'repository_data': {},
           'builds': [{'id': 7, 'repository': {'id': 'r', 'name': 'repo'}}],
           'build_data': {'7': {'tests': tests}}}
    outputs = RawDataExporter(None).export_data(raw, str(tmp_path / 'raw.xlsx'))
    rows = pd.read_excel(outputs['excel'], sheet_name='Test Results')
    assert rows.loc[0, 'status'] == 'unavailable'
    assert rows.loc[0, ['total_tests', 'passed_tests', 'failed_tests']].isna().all()
    assert tests['linked_runs'] == 1  # Source diagnosis is retained.
    client.get_paginated_results = AsyncMock(return_value=[
        {'build': {'id': 7}, 'totalTests': 10, 'passedTests': 8}])
    result = await BuildsClient(client).get_test_results('Alpha', 7)
    assert result['results'][0]['failed'] is None


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


@pytest.mark.asyncio
async def test_policy_projection_preserves_empty_and_rejects_malformed_scope():
    from plumbline.collection.projection import project_snapshot
    client = client_fixture()
    provider = ADOProvider(client, 'synthetic', clock=lambda: OBSERVED_AT)
    empty = project_snapshot(await provider.collect(['Alpha'], WINDOW))
    assert empty['repository_data']['r1']['policies']['statistics']['total_policies'] == 0
    client.get_policy_configurations = AsyncMock(return_value=[{'id': 1, 'type': {'id': 't'}, 'settings': {'scope': 'invalid'}}])
    raw = project_snapshot(await provider.collect(['Alpha'], WINDOW))
    assert any(item['error'] == 'malformed_policy_scope' for item in raw['completeness']['failures'])
    assert 'policies' not in raw['repository_data']['r1']


@pytest.mark.asyncio
async def test_invalid_list_items_and_test_counts_remain_partial_without_losing_valid_repos():
    client = client_fixture()
    client.get_repositories = AsyncMock(return_value=[None, {'id': 'good', 'name': 'good'}])
    client.get_pull_requests = AsyncMock(return_value=[])
    client.get_test_results = AsyncMock(return_value={'results': [{'total': 1, 'passed': 2}], 'unlinked_runs': 0})
    snap = await ADOProvider(client, 'c', clock=lambda: OBSERVED_AT).collect(['Alpha'], WINDOW)
    assert snap.projects[0].fields['repositories'].state == EvidenceState.PARTIAL
    assert [r.identity.repository for r in snap.repositories] == ['good']
    assert snap.builds[0].fields['tests'].state == EvidenceState.PARTIAL


@pytest.mark.asyncio
async def test_policy_progress_and_future_closure_are_uncertain():
    client = client_fixture()
    client.get_policy_types = AsyncMock(side_effect=RuntimeError('failure'))
    client.get_pull_requests = AsyncMock(return_value=[{'pullRequestId': 1, 'creationDate': '2025-12-01T00:00:00Z',
                                                        'closedDate': '2026-02-01T00:00:00Z', 'status': 'completed'}])
    progress = []
    snap = await ADOProvider(client, 'c', clock=lambda: OBSERVED_AT, progress_callback=progress.append).collect(['Alpha'], WINDOW)
    assert snap.repositories[0].fields['pull_requests'].error_code == 'historical_pr_status_unknown'
    assert progress[-1]['status'] == 'failed'
    from plumbline.collection.projection import project_snapshot
    assert 'policies' not in project_snapshot(snap)['repository_data']['r1']


def test_policy_scopes_resolve_actual_default_and_preserve_mixed_scope():
    from plumbline.collection.projection import _policies
    scopes = [{'repositoryId': None, 'matchKind': 'DefaultBranch', 'refName': None},
              {'repositoryId': 'r', 'matchKind': 'Prefix', 'refName': 'refs/heads/trunk/'},
              {'repositoryId': 'r', 'matchKind': 'Exact', 'refName': 'refs/heads/other'}]
    policy = {'id': 1, 'isEnabled': True, 'isBlocking': True, 'type': {'id': 't'}, 'settings': {'scope': scopes}}
    result = _policies([policy], [{'id': 't', 'displayName': 'Review'}], 'r', OBSERVED_AT, 'refs/heads/trunk')
    branches = result['branch_policies']
    assert {(b['branch_name'], b['is_default_branch'], b['is_project_level']) for b in branches} == {
        ('trunk', True, True), ('other', False, False), ('trunk/', False, False)}
    assert result['statistics']['project_level_policies'] == result['statistics']['repository_level_policies'] == 1
    with pytest.raises(ValueError, match='unknown_default_branch'):
        _policies([policy], [], 'r', OBSERVED_AT, None)


def test_policy_overlap_prefix_flags_and_empty_repository():
    from plumbline.collection.projection import _policies
    from plumbline.backend.services.assessors.security_compliance import SecurityComplianceAssessor

    policy = {'id': 4, 'isEnabled': True, 'isBlocking': True,
              'type': {'displayName': 'Review'}, 'settings': {'scope': [
                  {'repositoryId': None, 'matchKind': 'DefaultBranch'},
                  {'repositoryId': 'r', 'matchKind': 'Exact', 'refName': 'refs/heads/release/v1'},
                  {'repositoryId': 'r', 'matchKind': 'Prefix', 'refName': 'refs/heads/release'}]}}
    result = _policies([policy], [], 'r', OBSERVED_AT, 'refs/heads/release/v1')
    branch = result['branch_policies'][0]
    assert len(branch['policies']) == 1
    assert len(branch['policies'][0]['applicable_scopes']) == 3
    assessor = SecurityComplianceAssessor.__new__(SecurityComplianceAssessor)
    assert assessor._calculate_branch_protection_from_policies([branch]) == assessor._calculate_branch_protection_from_policies([{**branch, 'policies': [branch['policies'][0]]}])
    prefix = {**policy, 'settings': {'scope': [policy['settings']['scope'][2]]}}
    with pytest.raises(ValueError, match='ambiguous_policy_scope'):
        _policies([prefix], [], 'r', OBSERVED_AT, 'refs/heads/releases/v1')
    assert _policies([prefix], [], 'r', OBSERVED_AT, 'refs/heads/main')['branch_policies'][0]['is_default_branch'] is False
    assert _policies([policy], [], 'r', OBSERVED_AT, None, empty_repo=True)['branch_policies'][0]['branch_name'] == 'release/v1'
    with pytest.raises(ValueError, match='malformed_policy_flags'):
        _policies([{**policy, 'isEnabled': 'false'}], [], 'r', OBSERVED_AT, 'refs/heads/release/v1')
    for root in ('refs/heads', 'refs/heads/'):
        wildcard = {**prefix, 'settings': {'scope': [{'repositoryId': 'r', 'matchKind': 'Prefix', 'refName': root}]}}
        assert _policies([wildcard], [], 'r', OBSERVED_AT, 'refs/heads/trunk')['branch_policies'][0]['is_default_branch']
    ambiguous = {**prefix, 'settings': {'scope': [{'repositoryId': 'r', 'matchKind': 'Prefix', 'refName': 'refs/heads/tr'}]}}
    with pytest.raises(ValueError, match='ambiguous_policy_scope'):
        _policies([ambiguous], [], 'r', OBSERVED_AT, 'refs/heads/trunk')


@pytest.mark.asyncio
async def test_documented_coverage_and_policy_progress_caveats():
    from plumbline.collection.projection import project_snapshot
    client = client_fixture()
    client.get_code_coverage = AsyncMock(return_value={'value': [{'lastError': '', 'modules': [
        {'statistics': {'blocksCovered': 2, 'linesCovered': 4}}]}]})
    client.get_test_results = AsyncMock(return_value={'results': [], 'unlinked_runs': 0})
    progress = []
    snap = await ADOProvider(client, 'c', clock=lambda: OBSERVED_AT, progress_callback=progress.append).collect(['Alpha'], WINDOW)
    raw = project_snapshot(snap)
    assert raw['completeness']['coverage_denominator_unavailable'][0]['build_id'] == '7'
    assert any('no coverage percentage' in text for text in raw['completeness']['caveats'])
    repos = deepcopy(DATA['Alpha']['repos'])
    for repo in repos:
        repo.pop('defaultBranch', None)
    client.get_repositories = AsyncMock(return_value=repos)
    client.get_branches = AsyncMock(return_value=[{'name': 'trunk'}])
    await ADOProvider(client, 'c', clock=lambda: OBSERVED_AT, progress_callback=progress.append).collect(['Alpha'], WINDOW)
    assert progress[-1]['status'] == 'failed'
    assert {'field': 'policies', 'error': 'unknown_default_branch'} in progress[-1]['errors']


def test_optional_coverage_counts_and_error_are_not_invented():
    from plumbline.collection.projection import _coverage
    from plumbline.connections.execution import redact
    sample = {'value': [{'modules': [{'statistics': {'linesCovered': 4}}]}]}
    assert _coverage(sample)['value'][0]['modules'][0]['statistics'] == {'linesCovered': 4}
    assert _coverage({'value': [{'lastError': 'unavailable'}]})['value'][0]['lastError'] == 'unavailable'
    assert _coverage({'coverageData': [{'coverageStats': [{'label': 'Lines', 'covered': 0, 'total': 100}]}]})['value'][0]['modules'][0]['statistics'] == {'linesCovered': 0, 'linesNotCovered': 100}
    with pytest.raises(ValueError, match='malformed_coverage'):
        _coverage({'value': [{'modules': [{'statistics': {'linesCovered': -1}}]}]})
    assert redact('words  with spaces', "  '  '") == 'words  with spaces'


@pytest.mark.asyncio
async def test_completed_build_with_unknown_finish_is_retained_partial():
    client = client_fixture()
    build = deepcopy(DATA['Alpha']['build'])
    build.pop('finishTime')
    build['status'] = 'completed'
    client.get_builds = AsyncMock(return_value=[build, {'id': 500, 'status': 'inProgress'}])
    snap = await ADOProvider(client, 'synthetic', clock=lambda: OBSERVED_AT).collect(['Alpha'], WINDOW)
    assert len(snap.builds) == 1
    assert snap.projects[0].fields['builds'].error_code == 'unknown_event_time'
    assert snap.builds[0].fields['build'].error_code == 'incomplete_build'


@pytest.mark.asyncio
async def test_empty_repository_does_not_block_default_policy():
    from plumbline.collection.projection import project_snapshot
    client = client_fixture()
    repos = deepcopy(DATA['Alpha']['repos'])
    for repo in repos:
        repo.pop('defaultBranch', None)
    client.get_repositories = AsyncMock(return_value=repos)
    client.get_policy_configurations = AsyncMock(return_value=[{'id': 10, 'isEnabled': True, 'isBlocking': True,
        'type': {'id': 't'}, 'settings': {'scope': [{'repositoryId': None, 'matchKind': 'DefaultBranch'}]}}])
    client.get_test_results = AsyncMock(return_value={'results': [], 'unlinked_runs': 0})
    client.get_code_coverage = AsyncMock(return_value={})
    snap = await ADOProvider(client, 'synthetic', clock=lambda: OBSERVED_AT).collect(['Alpha'], WINDOW)
    raw = project_snapshot(snap)
    assert not any(f.get('error') == 'unknown_default_branch' for f in raw['completeness']['failures'])
    assert raw['repository_data'][repos[0]['id']]['policies']['branch_policies'] == []


def test_optional_coverage_does_not_create_legacy_measurement():
    from plumbline.backend.services.assessors.quality_assurance import QualityAssuranceAssessor
    from plumbline.collection.projection import _coverage
    assessor = QualityAssuranceAssessor.__new__(QualityAssuranceAssessor)
    builds = [{'id': 7, 'finishTime': OBSERVED_AT.isoformat(), 'repository': {'id': 'r'}}]
    partial = {'7': {'coverage': _coverage({'value': [{'modules': [{'statistics': {'linesCovered': 4}}]}]})}}
    assert assessor._calculate_coverage(builds, partial) == []
    zero = {'7': {'coverage': _coverage({'value': [{'modules': [{'statistics': {'linesCovered': 0, 'linesNotCovered': 100}}]}]})}}
    assert assessor._calculate_coverage(builds, zero)[0][0] == 0
