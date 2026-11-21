import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlmodel import Session

from plumbline.api.models import CollectionConfig
from plumbline.config.settings import Settings
from plumbline.connections.execution import ConnectionValidationError
from plumbline.connections.models import Connection
from plumbline.connections.service import encrypt
from plumbline.jobs import queue
from plumbline.jobs.models import Job
from plumbline.jobs.worker import run_once
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import RawMetric, init_engine_and_tables


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    migrate(url)
    engine = init_engine_and_tables(url)
    settings = Settings(azure_devops_org="server", azure_devops_pat="server-secret", exemptions_file=None)
    with Session(engine) as db:
        db.add(Connection(id="ado", provider="ado", name="ADO", organisation="saved", config={"projects": ["Alpha"]}, credential_ciphertext=encrypt("saved-secret"), capabilities=[]))
        db.add(Connection(id="syn", provider="synthetic", name="Demo", config={"projects": ["Alpha", "Beta"]}, capabilities=[]))
        db.commit()
    return engine, settings


def job(engine, run_id):
    with Session(engine) as db:
        return db.execute(select(Job).where(Job.run_id == run_id)).scalar_one()


def test_enqueue_validation_and_scope(setup):
    engine, settings = setup
    for config, message in [
        (CollectionConfig(connection_id="missing", project="Alpha"), "Connection not found"),
        (CollectionConfig(connection_id="ado", org="other", project="Alpha"), "organisation"),
        (CollectionConfig(connection_id="ado", project="Beta"), "outside connection scope"),
    ]:
        with pytest.raises(ConnectionValidationError, match=message):
            queue.enqueue(engine, config, settings)
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", all_projects=True), settings)
    assert job(engine, run_id).config["projects"] == ["Alpha"]
    assert job(engine, run_id).config["org"] == "saved"


def test_saved_connection_final_save_failure_preserves_database_error(setup, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError
    engine, settings = setup
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="syn", project="Alpha", write_files=False), settings)
    finish = queue.finish

    def fail_successful_save(engine, run_id, owner, status, *args, **kwargs):
        if status == "succeeded":
            raise OperationalError("private_statement", {"private_parameter": "sensitive"},
                                   RuntimeError("database is locked"))
        return finish(engine, run_id, owner, status, *args, **kwargs)

    monkeypatch.setattr(queue, "finish", fail_successful_save)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "failed"
    assert row.error == "Database operation failed"
    assert row.attempts == 1
    assert "private_statement" not in caplog.text
    assert "private_parameter" not in caplog.text


def test_synthetic_real_runner_files_db_and_snapshot(setup, tmp_path):
    engine, settings = setup
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="syn", all_projects=True, output_path=str(tmp_path / "metrics")), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "succeeded", row.error
    assert row.result["completeness"]["complete"] is False
    assert row.result["assessment_status"] == "blocked_incomplete_collection"
    assert row.result["snapshot_id"]
    with Session(engine) as db:
        raw = db.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one().raw_data
        states = [r["fields"]["tests"]["state"] for r in raw["evidence"]["builds"]]
        assert states == ["partial", "failed"]
    from plumbline.collection.store import load_snapshot
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, row.result["snapshot_id"])
        assert len(snapshot.repositories) == 3
        assert snapshot.builds[1].fields["tests"].state.value == "failed"
    files = list(tmp_path.rglob("*.json"))
    assert files
    document = json.loads(files[0].read_text())
    assert document["evidence"]["builds"][0]["fields"]["tests"]["state"] == "partial"
    assert not list(tmp_path.rglob("*maturity*"))


def test_deleted_and_undecryptable_connection_fail_safely(setup, monkeypatch):
    engine, settings = setup
    first = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert asyncio.run(run_once(engine, settings))
    assert job(engine, first).error == "Connection credential is unavailable"
    second = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)
    with Session(engine) as db:
        db.delete(db.get(Connection, "ado"))
        db.commit()
    assert asyncio.run(run_once(engine, settings))
    assert job(engine, second).error == "Connection not found"
    assert "secret" not in json.dumps(job(engine, first).config) + str(job(engine, first).result) + job(engine, first).error


def test_saved_ado_real_runner_mock_transport(setup, monkeypatch, caplog):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    engine, settings = setup
    calls = []
    async def request(client, url, session, params=None, **kwargs):
        calls.append((client.base_url, client.settings.azure_devops_pat.get_secret_value(), url))
        if url.endswith("/_apis/projects"):
            return {"value": [{"id": "p1", "name": "Alpha"}]}, {}
        if url.endswith("/_apis/projects/Alpha"):
            return {"id": "p1", "name": "Alpha"}, {}
        return {"value": []}, {}
    monkeypatch.setattr(AzureDevOpsClient, "_request_json", request)
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", all_projects=True, write_files=False, include_maturity=False), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "succeeded", row.error
    assert calls and all(org == "https://dev.azure.com/saved" and pat == "saved-secret" for org, pat, _ in calls)
    assert "saved-secret" not in json.dumps(row.config) + json.dumps(row.result) + str(row.error) + caplog.text


def test_unsupported_provider_and_secret_in_failure(setup, monkeypatch, caplog):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    engine, settings = setup
    with Session(engine) as db:
        db.add(Connection(id="other", provider="unknown", name="Other", config={}, capabilities=[]))
        db.commit()
    with pytest.raises(ConnectionValidationError, match="unsupported"):
        queue.enqueue(engine, CollectionConfig(connection_id="other", project="Alpha"), settings)

    async def request(client, url, session, params=None, **kwargs):
        raise RuntimeError("transport leaked saved-secret")
    monkeypatch.setattr(AzureDevOpsClient, "_request_json", request)
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == "failed"
    assert "saved-secret" not in json.dumps(row.config) + json.dumps(row.result) + str(row.error) + caplog.text


def test_partial_evidence_is_redacted_before_storage(setup):
    engine, settings = setup
    run_id = queue.enqueue(engine, CollectionConfig(connection_id="ado", project="Alpha", write_files=False), settings)

    async def executor(config, settings, run_id, persist_result):
        result = {"status": "succeeded", "completeness": {"complete": False}, "outputs": {}}
        persist_result({"raw_ado_data": {"failure": "upstream echoed saved-secret"}},
                       {"run_id": run_id}, result)
        return result

    assert asyncio.run(run_once(engine, settings, executor=executor))
    with Session(engine) as db:
        raw = db.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one().raw_data
        assert raw['failure'] == 'upstream echoed [REDACTED]'

@pytest.mark.parametrize('saved', [False, True])
def test_ado_provider_worker_snapshot_and_files(setup, monkeypatch, tmp_path, caplog, saved):
    """Exercise the HTTP client, provider, runner and fenced worker together."""
    from datetime import datetime, timezone, timedelta
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    from plumbline.collection.store import load_snapshot
    from sqlalchemy import text

    engine, settings = setup
    secret = 'saved-secret' if saved else 'server-secret'
    if saved:
        with Session(engine) as db:
            item = db.get(Connection, 'ado')
            item.config = {'projects': ['Alpha', 'Beta']}
            db.add(item)
            db.commit()
    now = datetime.now(timezone.utc)
    event = (now - timedelta(seconds=10)).isoformat()
    old = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc).isoformat()
    pr = {'pullRequestId': 11, 'creationDate': old, 'closedDate': event,
          'status': 'completed', 'title': 'echo ' + secret, 'reviewers': []}
    seen = []
    async def request(client, url, session, params=None, **kwargs):
        seen.append(url)
        project = 'Alpha' if '/Alpha/' in url or url.endswith('/Alpha') else 'Beta'
        if url.endswith('/_apis/projects/Alpha') or url.endswith('/_apis/projects/Beta'):
            return {'id': project, 'name': project, 'description': secret}, {}
        if url.endswith('/_apis/git/repositories'):
            return {'value': [{'id': 'repo-' + project, 'name': project}]}, {}
        if url.endswith('/pullrequests'):
            assert 'searchCriteria.minTime' not in params
            return {'value': [pr] if project == 'Alpha' else []}, {}
        if url.endswith('/pullrequests/11'):
            return dict(pr), {}
        if url.endswith('/_apis/build/builds'):
            return {'value': [{'id': 7, 'queueTime': event, 'repository': {'id': 'repo-' + project}}]}, {}
        if url.endswith('/_apis/test/codecoverage'):
            return {'value': [{'lineCoverage': 81, 'note': secret}]}, {}
        if url.endswith('/_apis/test/runs'):
            raise RuntimeError('test service echoed ' + secret)
        return {'value': []}, {}
    monkeypatch.setattr(AzureDevOpsClient, '_request_json', request)
    from plumbline.providers.ado import ADOProvider
    original_evidence = ADOProvider._evidence
    def evidence_with_detail(provider, value, source, window, *args, **kwargs):
        from dataclasses import replace
        item = original_evidence(provider, value, source, window, *args, **kwargs)
        return replace(item, detail='dummy detail ' + secret) if source == 'get_code_coverage' else item
    monkeypatch.setattr(ADOProvider, '_evidence', evidence_with_detail)
    config = CollectionConfig(connection_id='ado' if saved else None, projects=['Alpha', 'Beta'],
                              output_path=str(tmp_path / ('saved' if saved else 'env') / 'metrics'))
    run_id = queue.enqueue(engine, config, settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == 'succeeded', row.error
    assert row.result['assessment_status'] == 'blocked_incomplete_collection'
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, row.result['snapshot_id'])
        persisted = str(conn.execute(text('select payload from evidence_snapshots')).fetchall())
    assert [(b.identity.project, b.identity.build) for b in snapshot.builds] == [('Alpha', '7'), ('Beta', '7')]
    assert snapshot.repositories[0].fields['pull_requests'].value[0]['pullRequestId'] == 11
    assert snapshot.builds[0].fields['tests'].state.value == 'failed'
    assert snapshot.builds[0].fields['coverage'].detail == 'dummy detail [REDACTED]'
    assert snapshot.builds[0].fields['coverage'].value['value'][0]['lineCoverage'] == 81
    assert snapshot.collection_started_at <= snapshot.collection_completed_at == snapshot.window.as_of
    assert any(f['error'] == 'identity_collision' for f in row.result['completeness']['failures'])
    raw_file = next(tmp_path.rglob('*.json'))
    assert secret not in persisted + raw_file.read_text() + caplog.text + json.dumps(row.result)
    assert seen


def test_redaction_preserves_nested_snapshot_types():
    from dataclasses import replace
    from plumbline.connections.execution import redact
    from plumbline.providers.contract import Evidence, EvidenceState, Identity, Record, Snapshot
    from plumbline.providers.synthetic import WINDOW
    secret = 'dummy-secret'
    snapshot = Snapshot('c', WINDOW, frozenset(), projects=[Record(Identity('c', secret), {
        'field': Evidence({'nested': [secret]}, EvidenceState.PARTIAL, 'source', None, detail=secret)})])
    clean = redact(snapshot, secret)
    assert isinstance(clean, Snapshot) and isinstance(clean.projects[0].fields['field'], Evidence)
    assert secret not in repr(clean)
    assert replace(clean.projects[0].fields['field'], detail=None).state == EvidenceState.PARTIAL


def test_single_project_policies_and_exemptions_reach_maturity(setup, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    from plumbline.collection.store import load_snapshot

    engine, settings = setup
    exemptions = tmp_path / 'exemptions.yml'
    exemptions.write_text('''version: "1.0"
exemptions:
  Alpha:
    level: metric
    exempt_metrics: [policy_compliance]
    reason: synthetic policy
    owner: test
    effective_date: 2020-01-01
''')
    settings = settings.model_copy(update={'exemptions_file': exemptions})
    event = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    calls = []
    async def request(client, url, session, params=None, **kwargs):
        calls.append(url)
        if url.endswith('/_apis/projects/Alpha'):
            return {'id': 'p1', 'name': 'Alpha'}, {}
        if url.endswith('/_apis/git/repositories'):
            return {'value': [{'id': 'r1', 'name': 'repo', 'defaultBranch': 'refs/heads/main'}]}, {}
        if url.endswith('/_apis/policy/configurations'):
            return {'value': [{'id': 1, 'isEnabled': True, 'isBlocking': True,
                               'type': {'id': 't1'}, 'settings': {'scope': [{'repositoryId': 'r1', 'refName': 'refs/heads/main', 'matchKind': 'Exact'}]}}]}, {}
        if url.endswith('/_apis/policy/types'):
            return {'value': [{'id': 't1', 'displayName': 'Minimum number of reviewers'}]}, {}
        if url.endswith('/pullrequests'):
            return {'value': [{'pullRequestId': 11, 'creationDate': '2025-12-01T00:00:00Z',
                               'closedDate': event, 'status': 'completed', 'reviewers': []}]}, {}
        if url.endswith('/pullrequests/11'):
            return {'pullRequestId': 11, 'creationDate': '2025-12-01T00:00:00Z',
                    'closedDate': event, 'status': 'completed', 'reviewers': []}, {}
        return {'value': []}, {}
    monkeypatch.setattr(AzureDevOpsClient, '_request_json', request)
    run_id = queue.enqueue(engine, CollectionConfig(project='Alpha', output_path=str(tmp_path / 'output' / 'metrics')), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == 'succeeded', row.error
    assert row.result['assessment_status'] == 'completed'
    with Session(engine) as db:
        raw = db.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one().raw_data
    policies = raw['repository_data']['r1']['policies']
    assert policies['statistics']['total_policies'] == policies['statistics']['enabled_policies'] == 1
    file_raw = json.loads(next(path for path in tmp_path.rglob('*.json') if 'maturity' not in path.name).read_text())
    assert file_raw['repository_data']['r1']['policies']['branch_policies'][0]['policies'][0]['type']['displayName'] == 'Minimum number of reviewers'
    assert raw['repository_data']['r1']['pull_requests'][0]['pullRequestId'] == 11
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, row.result['snapshot_id'])
    assert snapshot.projects[0].fields['policy_configurations'].value[0]['id'] == 1
    maturity = json.loads(next(tmp_path.rglob('*maturity*.json')).read_text())
    security = maturity['repositories']['r1']['security_compliance']
    assert 'policy_compliance' not in security['metrics']
    assert any('policy_compliance is exempt' in item for item in security['explanation'])
    assert sum(url.endswith('/_apis/policy/configurations') for url in calls) == 1
    assert sum(url.endswith('/_apis/policy/types') for url in calls) == 1
    assert 'server-secret' not in json.dumps(raw) + json.dumps(maturity)


def test_mocked_ado_build_assessment_through_worker(setup, monkeypatch, tmp_path):
    """Real client through persisted assessment, with independently known 80% coverage."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    from plumbline.collection.store import load_snapshot

    engine, settings = setup
    exemption = tmp_path / 'exempt.yml'
    exemption.write_text('''version: "1.0"
exemptions:
  Alpha:
    level: metric
    exempt_metrics: [policy_compliance]
    reason: synthetic
    owner: test
    effective_date: 2020-01-01
''')
    settings = settings.model_copy(update={'exemptions_file': exemption})
    now = datetime.now(timezone.utc)
    event = (now - timedelta(minutes=2)).isoformat()
    old = datetime(now.year - 1, 12, 1, tzinfo=timezone.utc).isoformat()
    calls = []
    async def request(client, url, session, params=None, **kwargs):
        calls.append(url)
        if url.endswith("/_apis/build/builds"):
            assert params["queryOrder"] == "finishTimeDescending" and "minTime" in params
        if url.endswith("/_apis/test/runs"):
            assert params["includeRunDetails"] == "true"
            assert params["buildUri"].rsplit("/", 1)[-1] not in {"90", "91", "92"}
        if url.endswith("/_apis/test/codecoverage"):
            assert str(params["buildId"]) not in {"90", "91", "92"}
        if '/_apis/projects/' in url:
            return {'id': 'p1', 'name': 'Alpha'}, {}
        if url.endswith('/_apis/git/repositories'):
            return {'value': [{'id': 'r1', 'name': 'repo', 'defaultBranch': 'refs/heads/trunk'}]}, {}
        if url.endswith('/_apis/policy/configurations'):
            return {'value': [{'id': 1, 'isEnabled': True, 'isBlocking': True, 'type': {'id': 't'},
                               'settings': {'scope': [{'repositoryId': None, 'matchKind': 'DefaultBranch', 'refName': None}]}}]}, {}
        if url.endswith('/_apis/policy/types'):
            return {'value': [{'id': 't', 'displayName': 'Review'}]}, {}
        if url.endswith('/pullrequests'):
            return {'value': [{'pullRequestId': 10, 'creationDate': old, 'closedDate': event, 'status': 'completed'},
                              {'pullRequestId': 99, 'creationDate': old, 'closedDate': old, 'status': 'completed'}]}, {}
        if url.endswith('/pullrequests/10'):
            return {'pullRequestId': 10, 'creationDate': old, 'closedDate': event, 'status': 'completed',
                    'createdBy': {'id': 'author'}, 'reviewers': [{'id': 'reviewer', 'vote': 10}]}, {}
        if url.endswith('/_apis/build/builds'):
            return {'value': [{'id': i, 'queueTime': old, 'finishTime': event, 'status': 'completed',
                               'result': 'succeeded', 'repository': {'id': 'r1'}} for i in (7, 8)] + [{'id': 90, 'finishTime': old, 'status': 'completed'}, {'id': 91, 'status': 'inProgress'}, {'id': 92, 'finishTime': (now + timedelta(days=1)).isoformat(), 'status': 'completed'}]}, {}
        if url.endswith('/_apis/test/codecoverage'):
            if str(params.get('buildId')) == '7':
                return {'value': [{'modules': [{'statistics': {'linesCovered': 80, 'linesNotCovered': 20}}]}]}, {}
            return {'value': []}, {}
        if url.endswith('/_apis/test/runs'):
            if str(params['buildUri']).endswith('/7'):
                return {'value': [{'build': {'id': 7}, 'totalTests': 10, 'passedTests': 8, 'unanalyzedTests': 2}]}, {}
            return {'value': []}, {}
        return {'value': []}, {}
    monkeypatch.setattr(AzureDevOpsClient, '_request_json', request)
    run_id = queue.enqueue(engine, CollectionConfig(projects=['Alpha', 'Alias'], output_path=str(tmp_path / 'result' / 'metrics')), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.status == 'succeeded', row.error
    assert row.result['assessment_status'] == 'completed', row.result
    assert not any('/pullrequests/99' in url for url in calls)
    assert not any(f'/builds/{i}/' in url for i in (90, 91, 92) for url in calls)
    with Session(engine) as db:
        raw = db.execute(select(RawMetric).where(RawMetric.run_id == run_id)).scalar_one().raw_data
    assert len(raw['projects']) == 1 and len(raw['builds']) == 2
    assert raw['build_data']['8']['tests']['results'] == []
    assert raw['repository_data']['r1']['policies']['branch_policies'][0]['branch_name'] == 'trunk'
    with engine.connect() as conn:
        snap = load_snapshot(conn, row.result['snapshot_id'])
        assert snap.builds[1].fields['tests'].state.value == 'observed_empty'
        assert snap.builds[0].fields['security_scans'].state.value == 'partial'
        assert conn.execute(text("select count(*) from evidence_checkpoints where field='security_scans'")).scalar_one() == 0
    maturity = json.loads(next(tmp_path.rglob('*maturity*.json')).read_text())
    assert maturity['repositories']['r1']['quality_assurance']['metrics']['code_coverage'] == 80
    assert maturity['repositories']['r1']['quality_assurance']['metrics']['test_pass_rate'] == 80

    from plumbline.runner import MetricsServiceFactory
    class FailedAssessment:
        def assess_raw_data(self, raw):
            return {'repositories': {}, 'overall': {'explanation': ['Assessment failed']}}
    monkeypatch.setattr(MetricsServiceFactory, 'create_service', lambda *args, **kwargs: FailedAssessment())
    failed_id = queue.enqueue(engine, CollectionConfig(project='Alpha', output_path=str(tmp_path / 'failure' / 'metrics')), settings)
    assert asyncio.run(run_once(engine, settings))
    failed = job(engine, failed_id)
    assert failed.result['assessment_status'] == 'failed'
    assert failed.result['assessment_error']['code'] == 'assessment_failed'
    assert failed.result['snapshot_id'] and failed.result['outputs']['raw_json']
    assert not list((tmp_path / 'failure').rglob('*maturity*'))
    from plumbline.api.main import _run_record
    assert _run_record((failed_id, 'succeeded', None, None, None, None, failed.result))['assessment_error']['code'] == 'assessment_failed'


def test_environment_worker_redacts_normalised_and_basic_credential_on_exception(setup, caplog):
    import base64
    engine, settings = setup
    secret = "  'dummy-worker-secret'  "
    settings = settings.model_copy(update={'azure_devops_pat': __import__('pydantic').SecretStr(secret)})
    encoded = base64.b64encode(b':dummy-worker-secret').decode()
    run_id = queue.enqueue(engine, CollectionConfig(project='Alpha', write_files=False), settings)
    async def executor(*args, **kwargs):
        raise RuntimeError('failure dummy-worker-secret Basic ' + encoded)
    assert asyncio.run(run_once(engine, settings, executor=executor))
    row = job(engine, run_id)
    assert row.status == 'failed' and '[REDACTED]' in row.error
    assert 'dummy-worker-secret' not in row.error + caplog.text
    assert encoded not in row.error + caplog.text


def test_strict_exemptions_reject_nested_errors_and_bad_dates(tmp_path):
    from plumbline.config.exemptions import ExemptionManager
    from pathlib import Path
    path = tmp_path / 'bad.yml'
    template = '''version: "1"
exemptions:
  Alpha:
    level: repository
    reason: reason
    owner: owner
    effective_date: 2026-01-01
    expiration_date: 2025-01-01
    repositories: [{name: repo, exempt_metrics: [x], unknown: 1}]
'''
    path.write_text(template)
    with pytest.raises(ValueError):
        ExemptionManager(Path(path), strict=True)
    path.write_text(template.replace(', unknown: 1', ''))
    with pytest.raises(ValueError):
        ExemptionManager(Path(path), strict=True)


def test_real_provider_repository_failure_reaches_progress(setup, monkeypatch):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    engine, settings = setup
    async def request(client, url, session, params=None, **kwargs):
        if url.endswith('/_apis/projects/Alpha'):
            return {'id': 'p1', 'name': 'Alpha'}, {}
        if url.endswith('/_apis/git/repositories'):
            return {'value': [{'id': 'r1', 'name': 'repo'}]}, {}
        if url.endswith('/pullrequests'):
            raise RuntimeError('unavailable')
        return {'value': []}, {}
    monkeypatch.setattr(AzureDevOpsClient, '_request_json', request)
    run_id = queue.enqueue(engine, CollectionConfig(project='Alpha', write_files=False), settings)
    assert asyncio.run(run_once(engine, settings))
    row = job(engine, run_id)
    assert row.result['assessment_status'] == 'blocked_incomplete_collection'
    progress = row.result['repositories']['p1:r1']
    assert progress['status'] == 'failed' and any(e['field'] == 'pull_requests' for e in progress['errors'])
