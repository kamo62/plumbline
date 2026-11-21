from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from plumbline.api import main as api
from plumbline.auth.service import bootstrap
from plumbline.config.settings import Settings
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import session_for


@pytest.fixture
def application(tmp_path):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    migrate(url)
    with session_for(url) as db:
        bootstrap(db, 'admin', 'initial-password-123')
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(database_url=url)
    yield TestClient(api.app), url
    api.app.dependency_overrides.clear()


def login(client, password):
    csrf = client.get('/auth/csrf').json()['csrf_token']
    return client.post('/auth/login', headers={'x-csrf-token': csrf},
                       json={'username': 'admin', 'password': password})


def test_every_registered_route_denies_anonymous(application):
    client, _ = application
    public = {('GET', '/healthz'), ('GET', '/auth/csrf'), ('POST', '/auth/login')}
    checked = set()
    routes = [(method.upper(), path) for path, operations in api.app.openapi()['paths'].items()
              for method in operations if method.upper() in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}]
    routes += [('GET', '/docs'), ('GET', '/openapi.json'), ('GET', '/redoc')]
    for method, template in routes:
        if (method, template) in public:
            continue
        path = template
        for parameter in ('id', 'user_id', 'token_id', 'project', 'file_path'):
            path = path.replace('{' + parameter + '}', str(uuid4()))
        response = client.request(method, path)
        assert response.status_code == 401, (method, path, response.status_code)
        checked.add((method, template))
    assert {('GET', '/auth/audit'), ('GET', '/v1/runs'), ('POST', '/v1/runs')} <= checked
    assert client.get('/ui/').status_code == 200
    assert client.get('/ui/v1/runs').status_code == 401
    assert client.get('/v1/runs').status_code == 401
    assert client.get('/openapi.json').status_code == 401


def test_real_application_password_csrf_tokens_and_override(application, tmp_path, monkeypatch):
    client, url = application
    first = login(client, 'initial-password-123')
    assert first.status_code == 200
    assert "Secure" not in first.headers["set-cookie"]
    headers = {'x-csrf-token': first.json()['csrf_token']}
    assert client.get('/v1/runs').status_code == 403
    assert client.post('/auth/password', json={'current_password': 'initial-password-123', 'new_password': 'replacement-password-123'}).status_code == 403
    assert client.post('/auth/password', headers=headers, json={'current_password': 'initial-password-123', 'new_password': 'replacement-password-123'}).status_code == 200
    assert client.get('/v1/runs').status_code == 401
    second = login(client, 'replacement-password-123')
    headers = {'x-csrf-token': second.json()['csrf_token']}
    token = client.post('/auth/tokens', headers=headers, json={'role': 'viewer', 'expires_days': 1}).json()
    bearer = {'authorization': 'Bearer ' + token['token']}
    assert client.get('/v1/runs', headers=bearer).status_code == 200
    assert client.post('/v1/runs', headers=bearer, json={}).status_code == 403
    assert client.get('/auth/users', headers=bearer).status_code == 403
    assert client.get('/auth/audit', headers=bearer).status_code == 403
    from datetime import datetime, timezone
    from plumbline.persistence.models import Run
    run = uuid4()
    exports = (tmp_path / 'exports').resolve()
    directory = exports / str(run)
    directory.mkdir(parents=True)
    (directory / 'report.json').write_text('{}')
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(database_url=url, exports_dir=exports)
    with session_for(url) as db:
        db.add(Run(id=run, executed_at=datetime.now(timezone.utc), artifacts_dir=str(directory)))
        db.commit()
    assert client.get(f'/v1/runs/{run}/artifacts/report.json', headers=bearer).content == b'{}'
    assert client.get(f'/v1/runs/{run}/artifacts.zip', headers=bearer).status_code == 200
    assert client.delete('/auth/tokens/' + token['id'], headers=headers).status_code == 200
    assert client.get('/v1/runs', headers=bearer).status_code == 401
    other = f"sqlite:///{tmp_path / 'other.db'}"
    migrate(other)
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(database_url=other)
    assert client.get('/v1/runs').status_code == 401
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(database_url=url)
    assert client.get('/v1/runs').status_code == 200


def test_unmigrated_database_fails_startup(tmp_path):
    url = f"sqlite:///{tmp_path / 'unmigrated.db'}"
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(database_url=url)
    try:
        with pytest.raises(RuntimeError, match='Run plumbline migrate'):
            with TestClient(api.app):
                pass
    finally:
        api.app.dependency_overrides.clear()
