from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from plumbline.api import main as api
from plumbline.config.settings import Settings
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import Run, session_for


@pytest.fixture
def storage(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    migrate(url)
    exports = (tmp_path / 'exports').resolve()
    exports.mkdir()
    monkeypatch.setattr(api, 'EXPORTS_DIR', exports)
    return Settings(database_url=url), exports


def add_run(settings, run_id, status='succeeded', when=None, directory=None):
    when = when or datetime.now(timezone.utc)
    with session_for(settings.database_url) as db:
        db.add(Run(id=run_id, status=status, executed_at=when, started_at=when,
                   artifacts_dir=str(directory) if directory else None))
        db.commit()


def test_health_and_filtered_sorted_runs(storage):
    settings, _ = storage
    now = datetime.now(timezone.utc)
    old, new = uuid4(), uuid4()
    add_run(settings, old, when=now - timedelta(hours=2))
    add_run(settings, new, when=now)
    add_run(settings, uuid4(), status='queued')
    add_run(settings, uuid4(), status='running')
    assert api.healthz(settings)['queued_runs'] == 1
    assert api.healthz(settings)['active_runs'] == 1
    assert [r['id'] for r in api.list_runs(status='succeeded', settings=settings)] == [str(new), str(old)]
    with pytest.raises(HTTPException) as exc:
        api.get_run_status(uuid4(), settings)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_artifact_directory_scope_and_listing(storage, tmp_path):
    settings, exports = storage
    run = uuid4()
    add_run(settings, run, directory=tmp_path / 'outside')
    with pytest.raises(HTTPException) as exc:
        await api.list_artifacts(run, settings)
    assert exc.value.status_code == 403
    missing = uuid4()
    add_run(settings, missing, directory=exports / str(missing))
    with pytest.raises(HTTPException) as exc:
        await api.list_artifacts(missing, settings)
    assert exc.value.status_code == 404
    run = uuid4()
    directory = exports / str(run)
    (directory / 'reports').mkdir(parents=True)
    (directory / 'reports' / 'result.json').write_text('{}')
    add_run(settings, run, directory=directory)
    listing = await api.list_artifacts(run, settings)
    assert [item['name'] for item in listing['files']] == [f'{run}/reports/result.json']


def test_health_does_not_expose_database_error(monkeypatch, storage):
    settings, _ = storage
    def fail(_):
        raise RuntimeError('secret database URL')
    monkeypatch.setattr(api, 'get_engine', fail)
    response = api.healthz(settings)
    assert response.status_code == 503
    assert b'secret' not in response.body


def test_database_run_includes_job_output_and_completeness(storage):
    from plumbline.jobs.models import Job
    settings, _ = storage
    run = uuid4()
    add_run(settings, run)
    result = {'outputs': {'json': 'report.json'}, 'completeness': {'complete': False},
              'assessment_status': 'blocked_incomplete_collection'}
    with session_for(settings.database_url) as db:
        db.add(Job(run_id=run, config={}, status='succeeded', result=result,
                   created_at=datetime.now(timezone.utc)))
        db.commit()
    for record in (api.get_run_status(run, settings), api.list_runs(settings=settings)[0]):
        assert record['outputs'] == result['outputs']
        assert record['completeness'] == result['completeness']
        assert record['assessment_status'] == result['assessment_status']
