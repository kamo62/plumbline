"""Registry decisions through the mounted application and execution boundary."""
import asyncio
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

from plumbline.api import main as api
from plumbline.api.models import CollectionConfig
from plumbline.auth.models import ApiToken, User
from plumbline.auth.service import digest, now
from plumbline.config.settings import Settings
from plumbline.connections.execution import ConnectionValidationError, effective_config
from plumbline.connections.models import Connection
from plumbline.connections.service import run_doctor
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import session_for
from plumbline.providers import ADOProvider, SyntheticProvider
from plumbline.providers.registry import UnsupportedProvider


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'registry.db'}"
    migrate(url)
    monkeypatch.setenv("PLUMBLINE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with session_for(url) as db:
        for role in ("admin", "viewer"):
            db.add(User(id=role, username=role, password_hash="unused", role=role, active=True, force_change=False))
            db.flush()
            db.add(ApiToken(id=role, token_hash=digest(role), user_id=role, role=role,
                            expires_at=now() + timedelta(days=1)))
        db.commit()
    api.app.dependency_overrides[api.get_settings] = lambda: Settings(database_url=url)
    try:
        yield TestClient(api.app), url
    finally:
        api.app.dependency_overrides.clear()


def auth(role):
    return {"Authorization": f"Bearer {role}"}


def test_mounted_metadata_is_authenticated_static_and_safe(mounted):
    client, url = mounted
    with session_for(url) as db:
        db.add(Connection(id="private-id", provider="ado", name="private-name", organisation="private-org",
                          config={"projects": ["private-project"]}, credential_ciphertext="private-ciphertext",
                          capabilities=[]))
        db.commit()
    assert client.get("/connections/providers").status_code == 401
    response = client.get("/connections/providers", headers=auth("viewer"))
    assert response.status_code == 200
    data = response.json()
    assert [entry["id"] for entry in data] == ["ado", "synthetic"]
    assert [entry["status"] for entry in data] == ["implemented_live_unverified", "diagnostic_only"]
    assert data[0]["requirements"] == {"organisation": "required", "projects": "required",
                                       "credential": "required_for_execution"}
    assert data[1]["requirements"] == {"organisation": "not_supported", "projects": "optional",
                                       "credential": "not_supported"}
    assert all(set(entry) == {"id", "label", "status", "capabilities", "requirements"} for entry in data)
    assert data[0]["capabilities"] == sorted(ADOProvider(None, "test").capabilities)
    assert data[1]["capabilities"] == sorted(SyntheticProvider().capabilities)
    assert "private-" not in response.text


def test_unknown_provider_rejected_at_each_connection_boundary(mounted, monkeypatch):
    client, url = mounted
    response = client.post("/connections", headers=auth("admin"), json={"provider": "unknown", "name": "x"})
    assert response.status_code == 422 and response.json()["detail"] == "Connection provider is unsupported"
    with session_for(url) as db:
        db.add(Connection(id="unknown", provider="unknown", name="x", organisation="bad",
                          config={"projects": "not-a-list"}, credential_ciphertext="must-not-decrypt", capabilities=[]))
        db.commit()
        with pytest.raises(ConnectionValidationError, match="provider is unsupported"):
            effective_config(db, CollectionConfig(connection_id="unknown", project="Alpha"))
        item = db.get(Connection, "unknown")
        monkeypatch.setattr("plumbline.connections.service.decrypt", lambda _: pytest.fail("unexpected decryption"))
        with pytest.raises(UnsupportedProvider, match="provider is unsupported"):
            asyncio.run(run_doctor(item))
    with pytest.raises(ValidationError, match="provider is unsupported"):
        CollectionConfig(project="Alpha", connection_provider="unknown")
    patch = client.patch("/connections/unknown", headers=auth("admin"), json={"name": "renamed"})
    diagnostic = client.post("/connections/unknown/test", headers=auth("admin"))
    assert patch.status_code == diagnostic.status_code == 422
    assert patch.json()["detail"] == diagnostic.json()["detail"] == "Connection provider is unsupported"


def test_scope_and_input_validation_use_normalised_saved_projects(mounted):
    client, url = mounted
    payload = {"provider": "ado", "name": "Work", "organisation": "example-org",
               "config": {"projects": [" Alpha ", "Alpha", "Beta"]}}
    response = client.post("/connections", headers=auth("admin"), json=payload)
    assert response.status_code == 201
    assert response.json()["config"] == {"projects": ["Alpha", "Beta"]}
    identifier = response.json()["id"]
    with session_for(url) as db:
        config, _ = effective_config(db, CollectionConfig(connection_id=identifier, projects=[" Alpha ", "Alpha"]))
        assert config.projects == ["Alpha"] and config.org == "example-org" and config.connection_provider == "ado"
        with pytest.raises(ConnectionValidationError, match="outside connection scope"):
            effective_config(db, CollectionConfig(connection_id=identifier, project="Other"))
        item = db.get(Connection, identifier)
        item.config = {"projects": [" Alpha ", "Alpha"]}
        db.add(item)
        db.commit()
        config, _ = effective_config(db, CollectionConfig(connection_id=identifier, project="Alpha"))
        assert config.projects == ["Alpha"]
        item.config = {"projects": ["valid", "bad\nproject"]}
        db.add(item)
        db.commit()
        with pytest.raises(ConnectionValidationError, match="configuration is invalid"):
            effective_config(db, CollectionConfig(connection_id=identifier, project="valid"))
    for config in ({"projects": ["bad\nproject"]}, {"projects": ["x"], "base_url": "https://example.com"}):
        assert client.post("/connections", headers=auth("admin"), json={**payload, "config": config}).status_code == 422
    assert client.post("/connections", headers=auth("admin"), json={**payload, "organisation": "https://example.com"}).status_code == 422
    assert client.post("/connections", headers=auth("admin"), json={"provider": "synthetic", "name": "x", "credential": "no"}).status_code == 422
    assert client.patch(f"/connections/{identifier}", headers=auth("admin"),
                        json={"config": {"projects": ["x" * 129]}}).status_code == 422
    assert client.patch(f"/connections/{identifier}", headers=auth("admin"), json={"name": "renamed"}).status_code == 422


def test_actual_diagnostic_dispatch_synthetic_and_mocked_ado(mounted, monkeypatch, caplog):
    from plumbline.backend.api.ado_client import AzureDevOpsClient

    client, _ = mounted
    calls = []

    async def request(transport, url, session, params=None, **kwargs):
        calls.append((transport.base_url, transport.settings.azure_devops_pat.get_secret_value(), url))
        if url.endswith("/_apis/projects/Alpha"):
            return {"id": "p1", "name": "Alpha"}, {}
        return {"value": []}, {}

    monkeypatch.setattr(AzureDevOpsClient, "_request_json", request)
    synthetic = client.post("/connections", headers=auth("admin"), json={"provider": "synthetic", "name": "Demo"}).json()
    result = client.post(f"/connections/{synthetic['id']}/test", headers=auth("admin"))
    assert result.status_code == 200 and result.json()["test_status"] == "issues"
    checks = result.json()["test_result"]["checks"]
    assert any(check[0] == "tests" and check[1] == "partial" and check[3] == 1 for check in checks)
    assert any(check[0] == "tests" and check[1] == "failed" and check[3] == 1 for check in checks)
    assert not calls

    ado = client.post("/connections", headers=auth("admin"), json={"provider": "ado", "name": "Work",
                       "organisation": "example-org", "config": {"projects": ["Alpha"]},
                       "credential": "test-secret"}).json()
    result = client.post(f"/connections/{ado['id']}/test", headers=auth("admin"))
    assert result.status_code == 200 and result.json()["test_status"] == "passed", result.text
    assert calls and all(base == "https://dev.azure.com/example-org" and secret == "test-secret"
                         for base, secret, _ in calls)
    assert "test-secret" not in result.text and "credential_ciphertext" not in result.text

    async def failed_request(*args, **kwargs):
        import logging
        logging.getLogger('plumbline.backend.api.ado_client').error('Proxy echoed %s', 'test-secret')
        raise RuntimeError('test-secret')

    monkeypatch.setattr(AzureDevOpsClient, '_request_json', failed_request)
    result = client.post(f"/connections/{ado['id']}/test", headers=auth('admin'))
    assert result.status_code == 200 and result.json()['test_status'] == 'issues'
    assert 'test-secret' not in result.text + caplog.text
    assert 'Proxy echoed [REDACTED]' in caplog.text
