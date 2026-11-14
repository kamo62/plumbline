from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from plumbline.api import main as api
from plumbline.config.settings import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    from plumbline.persistence.migrations import migrate
    from plumbline.persistence.models import session_for
    from plumbline.auth.service import bootstrap
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(api, "EXPORTS_DIR", exports.resolve())
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    settings = Settings(azure_devops_pat="test-pat", azure_devops_org="test-org", database_url=url,
                        exemptions_file=tmp_path / "exemptions.yaml")
    with session_for(url) as db:
        user = bootstrap(db, "admin", "initial-password-123")
        user.force_change = False
        db.add(user)
        db.commit()
    api.app.dependency_overrides[api.get_settings] = lambda: settings
    http = TestClient(api.app)
    csrf = http.get("/auth/csrf").json()["csrf_token"]
    login = http.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "admin", "password": "initial-password-123"})
    http.headers["x-csrf-token"] = login.json()["csrf_token"]
    yield http, exports, settings
    api.app.dependency_overrides.clear()


def add_run(settings, run, directory):
    from datetime import datetime, timezone
    from plumbline.persistence.models import Run, session_for
    with session_for(settings.database_url) as db:
        db.add(Run(id=run, executed_at=datetime.now(timezone.utc), artifacts_dir=str(directory)))
        db.commit()


def test_downloads_reject_escape_and_foreign_run(client, tmp_path):
    http, exports, settings = client
    run, other = uuid4(), uuid4()
    directory = exports / str(run)
    directory.mkdir()
    outside = tmp_path / 'private.txt'
    outside.write_text('not an artifact')
    (directory / 'link').symlink_to(outside)
    (directory / 'sub').symlink_to(tmp_path, target_is_directory=True)
    add_run(settings, run, directory)
    headers = {}
    assert http.get(f'/v1/runs/{run}/artifacts/link', headers=headers).status_code == 403
    assert http.get(f'/v1/runs/{run}/artifacts/sub/private.txt', headers=headers).status_code == 403
    assert http.get(f'/v1/runs/{run}/artifacts/%2e%2e/private.txt', headers=headers).status_code == 403
    assert http.get(f'/v1/runs/{other}/artifacts/link', headers=headers).status_code == 404
    assert http.get(f'/v1/runs/{run}/artifacts', headers=headers).json()['files'] == []


def test_exemption_meta_upsert_dates_and_nested_rules(client):
    http, _, settings = client
    headers = {}
    path = settings.exemptions_file
    path.write_text(yaml.safe_dump({'version': '1.0', 'exemptions': {'Demo': {'level': 'repository', 'repositories': [{'name': 'repo', 'exempt_metrics': ['code_coverage']}], 'reason': 'old', 'owner': 'owner', 'effective_date': '2026-01-01'}}, 'pattern_exemptions': []}))
    assert 'metrics' in http.get('/v1/exemptions/meta').json()
    response = http.put('/v1/exemptions/demo', headers=headers, json={'reason': 'updated', 'effective_date': '2026-02-03', 'expiration_date': '2026-12-31'})
    assert response.status_code == 200, response.text
    rule = http.get('/v1/exemptions/Demo').json()['rule']
    assert rule['repositories'][0]['name'] == 'repo'
    assert rule['effective_date'] == '2026-02-03'
    assert list(yaml.safe_load(path.read_text())['exemptions']) == ['Demo']
    assert http.put('/v1/exemptions/demo', headers=headers, json={'effective_date': 'not-a-date'}).status_code == 422
    assert http.get('/v1/exemptions/demo').json()['rule'] == rule


def test_removed_routes(client):
    http, _, _ = client
    for method, path in [('post', '/v1/database/clear'), ('get', f'/v1/runs/{uuid4()}/validate'), ('get', '/v1/leaderboard'), ('get', '/v1/leaderboard/files')]:
        assert http.request(method, path).status_code == 404


def test_live_projects_use_existing_client_with_mocked_transport(client, monkeypatch):
    from plumbline.backend.api.ado_client import AzureDevOpsClient

    http, _, _ = client
    seen = []

    async def enter(self):
        seen.append(self.settings.azure_devops_org)
        return self

    async def exit_client(self, *args):
        pass

    async def get_projects(self):
        return [{'name': 'Synthetic B'}, {'name': 'Synthetic A'}]

    monkeypatch.setattr(AzureDevOpsClient, '__aenter__', enter)
    monkeypatch.setattr(AzureDevOpsClient, '__aexit__', exit_client)
    monkeypatch.setattr(AzureDevOpsClient, 'get_projects', get_projects)
    response = http.get('/v1/projects/live?org=test-org', headers={})
    assert response.status_code == 200
    assert response.json() == {'items': ['Synthetic A', 'Synthetic B']}
    assert seen == ['test-org']




def test_enqueue_persists_without_provider_calls(client, monkeypatch):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    http, _, _ = client
    monkeypatch.setattr(AzureDevOpsClient, '__init__', lambda *args, **kwargs: pytest.fail('provider called'))
    response = http.post('/v1/runs', json={'projects': ['one', 'two'], 'write_db': False, 'write_files': False})
    assert response.status_code == 202, response.text
    run = http.get('/v1/runs/' + response.json()['id'])
    assert run.status_code == 200
    assert run.json()['status'] == 'queued'


def test_run_progress_and_recovery_survive_api_reads(client):
    from sqlalchemy import update
    from plumbline.jobs.models import Job
    from plumbline.persistence.models import session_for

    http, _, settings = client
    run_id = http.post('/v1/runs', json={'project': 'one'}).json()['id']
    progress = {'one/repo-id': {'status': 'failed', 'errors': ['coverage_unavailable']}}
    with session_for(settings.database_url) as db:
        db.execute(update(Job).where(Job.run_id == run_id).values(result={
            'repositories': progress, 'attempt': 2, 'max_attempts': 3,
            'recovery_reason': 'previous attempt expired or stopped'}))
        db.commit()
    detail = http.get('/v1/runs/' + run_id).json()
    listing = http.get('/v1/runs').json()
    listed = next(item for item in listing if item['id'] == run_id)
    for record in (detail, listed):
        assert record['repositories'] == progress
        assert record['attempt'] == 2
        assert record['max_attempts'] == 3
        assert record['recovery_reason'] == 'previous attempt expired or stopped'


def test_missing_saved_connection_returns_actionable_error(client):
    http, _, _ = client
    response = http.post('/v1/runs', json={'connection_id': 'missing', 'project': 'Alpha'})
    assert response.status_code == 422
    assert response.json()['detail'] == 'Connection not found'
