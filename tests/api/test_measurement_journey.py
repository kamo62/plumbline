"""Mounted API and worker journey using inline data, never user response packs."""
import asyncio
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text

from plumbline import providers, runner
from plumbline.api import main as api
from plumbline.auth.service import bootstrap
from plumbline.backend.api.ado_client import AzureDevOpsClient
from plumbline.config.settings import Settings
from plumbline.jobs.worker import run_once
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import init_engine_and_tables, session_for


def test_saved_ado_worker_to_explicit_frozen_measurement(tmp_path, monkeypatch):
    fixed = datetime(2026, 2, 2, tzinfo=timezone.utc)

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    original_provider = providers.ADOProvider

    def provider(client, connection_id, **kwargs):
        return original_provider(client, connection_id, clock=lambda: fixed, **kwargs)

    calls = []

    async def request(client, url, session, params=None, **kwargs):
        calls.append(url)
        if url.endswith('/_apis/projects/Aster'):
            return {'id': 'project-1', 'name': 'Aster'}, {}
        if url.endswith('/_apis/git/repositories'):
            return {'value': [{'id': 'repo-1', 'name': 'synthetic_inline_repository',
                               'defaultBranch': 'refs/heads/main'}]}, {}
        if url.endswith('/_apis/build/builds'):
            return {'value': [{'id': 7, 'repository': {'id': 'repo-1'}, 'status': 'completed',
                               'result': 'succeeded', 'queueTime': '2025-12-31T23:59:00Z',
                               'startTime': '2026-01-15T00:00:00Z',
                               'finishTime': '2026-01-15T00:05:00Z',
                               'sourceBranch': 'refs/heads/main'}]}, {}
        if url.endswith('/timeline'):
            return {'id': '77777777-7777-4777-8777-777777777777', 'records': []}, {}
        if url.endswith('/_apis/test/runs'):
            return {'value': [{'id': 71, 'name': 'synthetic_inline_tests', 'build': {'id': '7'},
                               'state': 'Completed', 'totalTests': 20, 'passedTests': 18,
                               'unanalyzedTests': 2, 'incompleteTests': 0,
                               'notApplicableTests': 0}]}, {}
        return {'value': []}, {}

    monkeypatch.setattr(runner, 'datetime', FixedTime)
    monkeypatch.setattr(providers, 'ADOProvider', provider)
    monkeypatch.setattr(AzureDevOpsClient, '_request_json', request)
    monkeypatch.setenv('PLUMBLINE_ENCRYPTION_KEY', Fernet.generate_key().decode())
    url = f"sqlite:///{tmp_path / 'journey.db'}"
    migrate(url)
    engine = init_engine_and_tables(url)
    settings = Settings(database_url=url, azure_devops_org='server', azure_devops_pat='server-secret',
                        exemptions_file=None, exports_dir=tmp_path / 'exports')
    with session_for(url) as db:
        user = bootstrap(db, 'admin', 'initial-password-123')
        user.force_change = False
        db.add(user)
        db.commit()
    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_settings] = lambda: settings
    try:
        with TestClient(api.app) as http:
            csrf = http.get('/auth/csrf').json()['csrf_token']
            login = http.post('/auth/login', headers={'x-csrf-token': csrf},
                              json={'username': 'admin', 'password': 'initial-password-123'})
            assert login.status_code == 200
            http.headers['x-csrf-token'] = login.json()['csrf_token']
            connection = http.post('/connections', json={
                'provider': 'ado', 'name': 'synthetic_inline_connection', 'organisation': 'test-org',
                'config': {'projects': ['Aster']}, 'credential': 'synthetic-inline-secret',
            })
            assert connection.status_code == 201, connection.text
            queued = http.post('/v1/runs', json={
                'connection_id': connection.json()['id'], 'project': 'Aster',
                'write_files': False, 'include_maturity': False,
            })
            assert queued.status_code == 202, queued.text
            run_id = queued.json()['id']
            assert asyncio.run(run_once(engine, settings))
            run = http.get(f'/v1/runs/{run_id}').json()
            assert run['status'] == 'succeeded' and run['snapshot_id']
            assert run['assessment_status'] == 'not_requested' and run['outputs'] == {}
            collection_calls = list(calls)
            body = {'minimum_sample': 20, 'population_policy': 'ado-reported-total/v1'}
            created = http.post(f'/v1/runs/{run_id}/measurements/test-pass-rate', json=body)
            assert created.status_code == 200, created.text
            record = created.json()
            assert record['snapshot_id'] == run['snapshot_id']
            result, = record['document']['results']
            assert result['state'] == 'measured'
            assert (result['numerator'], result['denominator'], result['value']) == (18, 20, 90.0)
            assert result['coverage'] == {'observed': 1, 'expected': 1, 'percent': 100.0}
            location = f"/v1/measurements/{record['id']}"
            assert http.get(location).json() == record
            exported = http.get(location + '/export.json')
            assert exported.status_code == 200 and exported.json() == record
            assert 'attachment' in exported.headers['content-disposition']
            verification = http.get(location + '/verification').json()
            assert verification['content_intact'] is verification['snapshot_intact'] is True
            assert verification['reproducible'] is True and verification['status'] == 'verified'
            assert http.post(f'/v1/runs/{run_id}/measurements/test-pass-rate', json=body).json() == record
            assert calls == collection_calls  # All diagnostic operations use stored evidence only.
            assert not settings.exports_dir.exists()  # JSON export creates no server-side report.

            # Isolate the measurement guard from older raw/link guards.
            with engine.begin() as conn:
                for table in ('raw_metrics', 'metrics', 'run_evidence'):
                    conn.execute(text(f'DELETE FROM {table} WHERE run_id=:id'), {'id': run_id})
            assert http.delete(f'/v1/runs/{run_id}/data').status_code == 409
            assert http.get(location).json() == record
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)
