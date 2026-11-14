from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import select

from plumbline.auth.guard import install_guard
from plumbline.auth.models import ApiToken, User
from plumbline.auth.service import digest, now
from plumbline.connections.models import Connection
from plumbline.connections.router import create_router
from plumbline.connections.service import CredentialError, decrypt, encrypt, rotate
from plumbline.persistence.baseline import upgrade as baseline
from plumbline.persistence.models import init_engine_and_tables, session_for
from plumbline.persistence.revision_0003_auth import upgrade as auth_upgrade
from plumbline.persistence.revision_0004_connections import upgrade


@pytest.fixture
def setup(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'connections.db'}"
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", key)
    engine = init_engine_and_tables(url)
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        baseline(op)
        auth_upgrade(op)
        upgrade(op)
    with session_for(url) as db:
        for role in ("admin", "viewer"):
            db.add(User(id=role, username=role, password_hash="unused", role=role, active=True, force_change=False))
            db.flush()
            db.add(ApiToken(id=role, token_hash=digest(role), user_id=role, role=role,
                            expires_at=now() + timedelta(days=1)))
        db.commit()
    def get_db():
        with session_for(url) as db:
            yield db
    app = FastAPI()
    app.include_router(create_router(get_db))
    install_guard(app, url)
    return TestClient(app), url, key


def auth(role):
    return {"Authorization": f"Bearer {role}"}


def test_encryption_redaction_update_and_policy(setup, monkeypatch):
    client, url, key = setup
    payload = {"provider": "ado", "name": "Work", "organisation": "example-org",
               "config": {"projects": ["Project"]}, "credential": "private-pat-123"}
    assert client.get("/connections").status_code == 401
    assert client.post("/connections", json=payload, headers=auth("viewer")).status_code == 403
    response = client.post("/connections", json=payload, headers=auth("admin"))
    assert response.status_code == 201, response.text
    item_id = response.json()["id"]
    with session_for(url) as db:
        row = db.get(Connection, item_id)
        ciphertext = row.credential_ciphertext
        assert decrypt(ciphertext, key) == payload["credential"]
        raw = db.exec(text("SELECT credential_ciphertext FROM connections")).first()[0]
        assert payload["credential"] not in raw
    listed = client.get("/connections", headers=auth("viewer"))
    assert listed.status_code == 200
    assert payload["credential"] not in listed.text and ciphertext not in listed.text
    assert "credential" not in listed.text
    assert client.patch(f"/connections/{item_id}", json={"name": "Renamed"}, headers=auth("admin")).status_code == 200
    with session_for(url) as db:
        assert db.get(Connection, item_id).credential_ciphertext == ciphertext
    assert client.patch(f"/connections/{item_id}", json={"credential": None}, headers=auth("admin")).status_code == 422
    assert client.post(f"/connections/{item_id}/test", headers=auth("viewer")).status_code == 403
    replacement = client.patch(f"/connections/{item_id}", json={"credential": "replacement-pat"}, headers=auth("admin"))
    assert replacement.status_code == 200 and "replacement-pat" not in replacement.text
    with session_for(url) as db:
        assert decrypt(db.get(Connection, item_id).credential_ciphertext, key) == "replacement-pat"
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    failed = client.post(f"/connections/{item_id}/test", headers=auth("admin"))
    assert failed.json()["test_result"]["checks"][0][2] == "credential_unavailable"
    assert payload["credential"] not in failed.text and ciphertext not in failed.text
    assert client.post("/connections", json={**payload, "organisation": "https://evil.test/"}, headers=auth("admin")).status_code == 422


def test_synthetic_doctor(setup):
    client, _, _ = setup
    item = client.post("/connections", json={"provider": "synthetic", "name": "Demo"}, headers=auth("admin")).json()
    response = client.post(f"/connections/{item['id']}/test", headers=auth("admin"))
    assert response.status_code == 200
    result = response.json()
    assert result["test_status"] == "issues"
    assert result["capabilities"] and result["test_result"]["checks"]
    assert "pullRequestId" not in response.text


def test_rotation_is_atomic(setup):
    _, url, old = setup
    new = Fernet.generate_key().decode()
    with session_for(url) as db:
        for id_, key in (("one", old), ("two", Fernet.generate_key().decode())):
            db.add(Connection(id=id_, provider="ado", name=id_, organisation="org", config={"projects": ["p"]},
                              credential_ciphertext=encrypt("secret-" + id_, key), capabilities=[], test_status="untested"))
        db.commit()
    with session_for(url) as db:
        before = [r.credential_ciphertext for r in db.exec(select(Connection).order_by(Connection.id)).all()]
    with session_for(url) as db, pytest.raises(CredentialError):
        rotate(db, old, new)
    with session_for(url) as db:
        rows = db.exec(select(Connection).order_by(Connection.id)).all()
        assert [r.credential_ciphertext for r in rows] == before
        rows[1].credential_ciphertext = encrypt("secret-two", old)
        db.add(rows[1])
        db.commit()
    with session_for(url) as db:
        assert rotate(db, old, new) == 2
    with session_for(url) as db:
        rows = db.exec(select(Connection).order_by(Connection.id)).all()
        assert [decrypt(row.credential_ciphertext, new) for row in rows] == ["secret-one", "secret-two"]
        with pytest.raises(CredentialError):
            decrypt(rows[0].credential_ciphertext, old)


def test_ado_doctor_with_mock_client(setup, monkeypatch):
    client, _, _ = setup
    seen = []

    class FakeClient:
        def __init__(self, settings):
            seen.append((settings.azure_devops_org, settings.azure_devops_pat.get_secret_value()))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_project(self, project):
            raise RuntimeError("sensitive source exception private-pat-123")

    monkeypatch.setattr("plumbline.backend.api.ado_client.AzureDevOpsClient", FakeClient)
    response = client.post("/connections", headers=auth("admin"), json={
        "provider": "ado", "name": "Work", "organisation": "example-org",
        "config": {"projects": ["Project"]}, "credential": "private-pat-123",
    })
    tested = client.post(f"/connections/{response.json()['id']}/test", headers=auth("admin"))
    assert seen == [("example-org", "private-pat-123")]
    assert tested.json()["test_result"]["checks"][0][2] == "request_failed"
    assert "private-pat-123" not in tested.text and "sensitive source exception" not in tested.text


def test_rotation_cli_from_environment(setup, monkeypatch):
    import typer
    from typer.testing import CliRunner
    from plumbline.connections_cli import register

    _, url, old = setup
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("PLUMBLINE_DATABASE_URL", url)
    monkeypatch.setenv("PLUMBLINE_OLD_ENCRYPTION_KEY", old)
    monkeypatch.setenv("PLUMBLINE_NEW_ENCRYPTION_KEY", new)
    with session_for(url) as db:
        db.add(Connection(id="one", provider="ado", name="one", organisation="org", config={"projects": ["p"]},
                          credential_ciphertext=encrypt("secret-one", old), capabilities=[], test_status="untested"))
        db.commit()
    app = typer.Typer()
    register(app)
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "1 connection(s)" in result.output
    assert old not in result.output and new not in result.output and "secret-one" not in result.output
    with session_for(url) as db:
        assert decrypt(db.get(Connection, "one").credential_ciphertext, new) == "secret-one"
