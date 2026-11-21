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
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    settings = Settings(azure_devops_pat="test-pat", azure_devops_org="test-org", database_url=url,
                        exports_dir=exports,
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
    original = path.read_bytes()
    for body in ({'expiration_date': '2020-01-01'}, {'repositories': [{'name': 'repo', 'unknown': 1}]}):
        assert http.put('/v1/exemptions/demo', json=body).status_code == 422
        assert path.read_bytes() == original


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


def test_worker_attempt_reports_are_downloadable_and_isolated(client):
    import io
    import zipfile

    http, exports, settings = client
    run, attempt, previous = uuid4(), uuid4(), uuid4()
    directory = exports / str(run) / str(attempt)
    directory.mkdir(parents=True)
    (directory / 'report.json').write_text('{"sample": true}')
    sibling = directory.parent / str(previous)
    sibling.mkdir()
    (sibling / 'stale.json').write_text('{}')
    add_run(settings, run, directory)
    listing = http.get(f'/v1/runs/{run}/artifacts')
    assert listing.status_code == 200, listing.text
    files = listing.json()['files']
    assert len(files) == 1
    assert http.get(files[0]['url']).json() == {'sample': True}
    response = http.get(f'/v1/runs/{run}/artifacts.zip')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ['report.json']
        assert archive.read('report.json') == b'{"sample": true}'


@pytest.mark.parametrize('layout', ['foreign', 'nested', 'symlink', 'non_uuid', 'owner_symlink', 'traversal', 'uppercase'])
def test_recorded_artifact_directory_requires_exact_ownership(client, layout):
    http, exports, settings = client
    run = uuid4()
    owner = exports / str(run)
    owner.mkdir()
    foreign = exports / str(uuid4())
    foreign.mkdir()
    if layout == 'foreign':
        directory = foreign
    elif layout == 'nested':
        directory = owner / str(uuid4()) / str(uuid4())
        directory.mkdir(parents=True)
    elif layout == 'non_uuid':
        directory = owner / 'unowned'
        directory.mkdir()
    elif layout == 'traversal':
        attempt = str(uuid4())
        (owner / attempt).mkdir()
        directory = owner / attempt / '..' / attempt
    elif layout == 'uppercase':
        directory = owner / str(uuid4()).upper()
        directory.mkdir()
    elif layout == 'owner_symlink':
        owner.rmdir()
        owner.symlink_to(foreign, target_is_directory=True)
        directory = owner
    else:
        directory = owner / str(uuid4())
        directory.symlink_to(foreign, target_is_directory=True)
    add_run(settings, run, directory)
    for suffix in ('artifacts', 'artifacts.zip'):
        assert http.get(f'/v1/runs/{run}/{suffix}').status_code == 403


def test_api_rejects_custom_output_destination_before_enqueue(client, monkeypatch):
    http, _, _ = client
    monkeypatch.setattr(api.queue, 'enqueue', lambda *args: pytest.fail('invalid request enqueued'))
    response = http.post('/v1/runs', json={'project': 'one', 'output_path': '/tmp/arbitrary/report'})
    assert response.status_code == 422


@pytest.mark.parametrize('field,value', [
    ('connection_provider', 'synthetic'), ('connection_provider', None),
    ('db_strict', True), ('db_strict', False), ('db_strict', None),
])
def test_api_rejects_internal_controls_before_enqueue(client, monkeypatch, field, value):
    http, _, _ = client
    monkeypatch.setattr(api.queue, 'enqueue', lambda *args: pytest.fail('invalid request enqueued'))
    assert http.post('/v1/runs', json={'project': 'one', field: value}).status_code == 422


def test_internal_collection_config_still_accepts_internal_controls():
    from plumbline.api.models import CollectionConfig
    config = CollectionConfig(project='one', connection_provider='synthetic', db_strict=True,
                              output_path='custom/metrics')
    assert config.connection_provider == 'synthetic'
    assert config.db_strict is True
    assert config.output_path == 'custom/metrics'


def test_openapi_excludes_internal_collection_controls():
    schema = api.app.openapi()['components']['schemas']['RunRequest']
    assert 'connection_provider' not in schema['properties']
    assert 'db_strict' not in schema['properties']
    assert schema['properties']['output_path']['type'] == 'null'
    assert schema['additionalProperties'] is False
