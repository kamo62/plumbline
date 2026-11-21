"""Synthetic saved-connection journey through the actual API, worker and file sink."""
import asyncio
import io
import json
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from plumbline.api import main as api
from plumbline.auth.service import bootstrap
from plumbline.config.settings import Settings
from plumbline.jobs.worker import run_once
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import init_engine_and_tables, session_for


def test_exports_setting_is_anchored_when_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PLUMBLINE_EXPORTS_DIR', raising=False)
    assert Settings(exemptions_file=None).exports_dir == tmp_path / 'exports'
    monkeypatch.setenv('PLUMBLINE_EXPORTS_DIR', 'configured-reports')
    settings = Settings(exemptions_file=None)
    assert settings.exports_dir == tmp_path / 'configured-reports'
    monkeypatch.setenv('PLUMBLINE_EXPORTS_DIR', '')
    assert Settings(exemptions_file=None).exports_dir == tmp_path / 'exports'
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.chdir(other)
    assert settings.exports_dir == tmp_path / 'configured-reports'


def test_cli_relative_custom_output_uses_custom_parent(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    from plumbline.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PLUMBLINE_AZURE_DEVOPS_PAT', 'test-pat')
    monkeypatch.setenv('PLUMBLINE_AZURE_DEVOPS_ORG', 'test-org')
    monkeypatch.setenv('PLUMBLINE_EXEMPTIONS_FILE', '')
    monkeypatch.setenv('PLUMBLINE_EXPORTS_DIR', str(tmp_path / 'configured-exports'))

    async def request(client, url, session, params=None, **kwargs):
        if url.endswith('/_apis/projects/Alpha'):
            return {'id': 'p1', 'name': 'Alpha'}, {}
        return {'value': []}, {}

    monkeypatch.setattr(AzureDevOpsClient, '_request_json', request)
    result = CliRunner().invoke(app, ['collect-raw', '--project', 'Alpha', '--output', 'custom/report'])
    assert result.exit_code == 0, result.output
    raw_files = list((tmp_path / 'custom').glob('*/report.json'))
    assert len(raw_files) == 1
    assert str(UUID(raw_files[0].parent.name)) == raw_files[0].parent.name
    assert not (tmp_path / 'configured-exports').exists()


def test_saved_synthetic_run_downloads_across_working_directories(tmp_path, monkeypatch):
    api_cwd, worker_cwd = tmp_path / 'api', tmp_path / 'worker'
    api_cwd.mkdir()
    worker_cwd.mkdir()
    actual_parent = tmp_path / 'real-parent'
    actual_parent.mkdir()
    linked_parent = tmp_path / 'linked-parent'
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    configured_exports = linked_parent / 'shared-exports'
    exports = actual_parent / 'shared-exports'
    url = f"sqlite:///{tmp_path / 'test.db'}"
    migrate(url)
    monkeypatch.chdir(api_cwd)
    settings = Settings(database_url=url, azure_devops_org='server', exemptions_file=None,
                        exports_dir=configured_exports)
    assert settings.exports_dir == exports
    with session_for(url) as db:
        admin = bootstrap(db, 'admin', 'initial-password-123')
        admin.force_change = False
        db.add(admin)
        db.commit()
    api.app.dependency_overrides[api.get_settings] = lambda: settings
    try:
        http = TestClient(api.app)
        assert http.get('/v1/runs').status_code == 401
        csrf = http.get('/auth/csrf').json()['csrf_token']
        login = http.post('/auth/login', headers={'x-csrf-token': csrf},
                          json={'username': 'admin', 'password': 'initial-password-123'})
        assert login.status_code == 200
        http.headers['x-csrf-token'] = login.json()['csrf_token']
        connection = http.post('/connections', json={'provider': 'synthetic', 'name': 'Offline',
                                                        'config': {'projects': ['Alpha']}})
        assert connection.status_code == 201, connection.text
        response = http.post('/v1/runs', json={'connection_id': connection.json()['id'],
                                               'project': 'Alpha'})
        assert response.status_code == 202, response.text
        run = response.json()['id']
        assert http.get(f'/v1/runs/{run}').json()['status'] == 'queued'

        monkeypatch.chdir(worker_cwd)
        worker_settings = Settings(database_url=url, azure_devops_org='server', exemptions_file=None,
                                   exports_dir=configured_exports)
        engine = init_engine_and_tables(url, allow_create_all=False)
        assert asyncio.run(run_once(engine, worker_settings))
        with engine.connect() as conn:
            stored = conn.execute(text('SELECT artifacts_dir FROM runs WHERE id=:id'), {'id': run}).scalar_one()
            stored_result = conn.execute(text('SELECT result FROM jobs WHERE run_id=:id'), {'id': run}).scalar_one()
        directory = Path(stored)
        assert directory.is_absolute() and directory.parent == exports / run
        assert str(UUID(directory.name)) == directory.name
        assert not (worker_cwd / 'exports').exists()

        monkeypatch.chdir(api_cwd)
        detail = http.get(f'/v1/runs/{run}').json()
        assert detail['status'] == 'succeeded'
        assert detail['artifacts_dir'] == f'{run}/{directory.name}'
        assert detail['completeness']['complete'] is False
        assert detail['assessment_status'] == 'blocked_incomplete_collection'
        assert detail['snapshot_id']
        outputs = json.loads(stored_result)['outputs'] if isinstance(stored_result, str) else stored_result['outputs']
        assert all(Path(path).is_absolute() for path in outputs.values())
        raw_path = Path(outputs['raw_json'])
        assert raw_path.parent == directory
        assert detail['outputs']['raw_json'] == raw_path.name
        listed = next(item for item in http.get('/v1/runs').json() if item['id'] == run)
        assert listed['artifacts_dir'] == detail['artifacts_dir']
        assert listed['outputs'] == detail['outputs']
        raw_bytes = raw_path.read_bytes()
        raw = json.loads(raw_bytes)
        assert raw['evidence']['builds'][0]['fields']['tests']['state'] == 'partial'

        sibling = directory.parent / str(uuid4())
        sibling.mkdir()
        (sibling / 'stale.json').write_bytes(b'stale sibling')
        (directory / 'linked-file').symlink_to(raw_path)
        (directory / 'linked-dir').symlink_to(sibling, target_is_directory=True)
        listing = http.get(f'/v1/runs/{run}/artifacts')
        assert listing.status_code == 200, listing.text
        files = listing.json()['files']
        assert listing.json()['artifacts_dir'] == f'{run}/{directory.name}'
        assert files and all(not item['name'].startswith((run, 'linked-')) for item in files)
        assert all('stale.json' not in item['name'] for item in files)
        raw_file = next(item for item in files if item['name'].endswith(raw_path.name))
        assert http.get(raw_file['url']).content == raw_bytes
        assert http.get(f'/v1/runs/{run}/artifacts/linked-file').status_code == 403
        assert http.get(f'/v1/runs/{run}/artifacts/linked-dir/stale.json').status_code == 403
        assert http.get(f'/v1/runs/{run}/artifacts/%2e%2e/{sibling.name}/stale.json').status_code == 403
        assert http.get(f'/v1/runs/{uuid4()}/artifacts/{raw_path.name}').status_code == 404
        zipped = http.get(f'/v1/runs/{run}/artifacts.zip')
        assert zipped.status_code == 200
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            assert archive.read(raw_path.name) == raw_bytes
            assert 'stale.json' not in archive.namelist()
            assert 'linked-file' not in archive.namelist()
    finally:
        api.app.dependency_overrides.clear()
