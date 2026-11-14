import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlmodel import select

from plumbline.auth import guard
from plumbline.auth.models import AuditEvent, User
from plumbline.auth.oidc import OIDCConfig, create_oidc_router
from plumbline.auth.oidc_models import OIDCIdentity
from plumbline.auth.router import create_router
from plumbline.auth.service import bootstrap
from plumbline.persistence.models import init_engine_and_tables, session_for
from plumbline.persistence.revision_0003_auth import upgrade as auth_upgrade
from plumbline.persistence.revision_0006_oidc import upgrade as oidc_upgrade

ISSUER = "https://issuer.example.com/tenant/v2.0"
CLIENT = "client-id"


@pytest.fixture
def flow(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'oidc.db'}"
    engine = init_engine_and_tables(url)
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        auth_upgrade(op)
        oidc_upgrade(op)
    with session_for(url) as db:
        admin = bootstrap(db, "admin", "initial-password-123")
        admin.force_change = False
        db.add(admin)
        db.commit()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="key1", use="sig", alg="RS256")
    state = {"claims": {}, "key": key, "nonce": "", "challenge": "", "subject": "subject-1"}

    def provider(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                                             "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/keys"})
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [jwk]})
        assert request.url.path.endswith("/token")
        form = parse_qs(request.content.decode())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert challenge == state["challenge"]
        assert form["code"] == ["valid-code"]
        claims = {"iss": ISSUER, "sub": state["subject"], "aud": CLIENT, "iat": int(time.time()),
                  "exp": int(time.time()) + 300, "nonce": state["nonce"], "email": "admin@example.com",
                  "roles": ["admin"]}
        claims.update(state["claims"])
        token = jwt.encode(claims, state["key"], algorithm="RS256", headers={"kid": "key1"})
        return httpx.Response(200, json={"id_token": token, "access_token": "never-store-me"})

    def factory():
        return httpx.Client(transport=httpx.MockTransport(provider))

    def get_db():
        with session_for(url) as db:
            yield db
    app = FastAPI()
    app.include_router(create_router(get_db, secure_cookie=True))
    app.include_router(create_oidc_router(get_db, OIDCConfig(ISSUER, CLIENT, "secret", "https://app.example.com/auth/oidc/callback"), client_factory=factory))
    @app.get("/private")
    def private(request: Request):
        return request.state.principal
    @app.post("/private")
    def mutate():
        return {"ok": True}
    monkeypatch.setattr(guard, "PUBLIC", guard.PUBLIC | {("GET", "/auth/oidc/start"), ("GET", "/auth/oidc/callback")})
    guard.install_guard(app, url)
    client = TestClient(app, base_url="https://app.example.com", follow_redirects=False)

    def start():
        response = client.get("/auth/oidc/start")
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        state["nonce"], state["challenge"] = query["nonce"][0], query["code_challenge"][0]
        assert query["code_challenge_method"] == ["S256"]
        return query["state"][0]
    def finish(value):
        return client.get("/auth/oidc/callback", params={"state": value, "code": "valid-code"})
    return client, url, state, other, start, finish


def test_flow_role_mapping_disable_and_revocation(flow):
    client, url, state, _, start, finish = flow
    value = start()
    assert finish(value).status_code == 303
    assert finish(value).status_code == 400
    principal = client.get("/private").json()
    assert principal["role"] == "viewer"
    csrf = client.cookies["plumbline_csrf"]
    assert client.post("/private", headers={"x-csrf-token": csrf}).status_code == 403
    with session_for(url) as db:
        identity = db.exec(select(OIDCIdentity)).one()
        user_id = identity.user_id
        assert db.get(User, user_id).username != "admin@example.com"
        audit = str(db.exec(select(AuditEvent)).all())
        assert "never-store-me" not in audit and "secret" not in audit
    # Existing authenticated administrator controls the role and revokes the OIDC session.
    admin_client = TestClient(client.app, base_url="https://app.example.com", follow_redirects=False)
    admin_csrf = admin_client.get("/auth/csrf").json()["csrf_token"]
    login = admin_client.post("/auth/login", headers={"x-csrf-token": admin_csrf},
                              json={"username": "admin", "password": "initial-password-123"})
    admin_headers = {"x-csrf-token": login.json()["csrf_token"]}
    assert admin_client.patch(f"/auth/users/{user_id}", headers=admin_headers, json={"active": True, "role": "admin"}).status_code == 200
    assert client.get("/private").status_code == 401
    assert finish(start()).status_code == 303
    assert client.get("/private").json()["role"] == "admin"
    assert admin_client.patch(f"/auth/users/{user_id}", headers=admin_headers, json={"active": False, "role": "viewer"}).status_code == 200
    assert client.get("/private").status_code == 401
    assert finish(start()).status_code == 403


@pytest.mark.parametrize("failure", ["state", "nonce", "issuer", "audience", "signature", "expired"])
def test_invalid_responses(flow, failure):
    client, _, state, other, start, finish = flow
    value = start()
    if failure == "state":
        assert finish("wrong").status_code == 400
        return
    if failure == "nonce":
        state["claims"]["nonce"] = "wrong"
    elif failure == "issuer":
        state["claims"]["iss"] = "https://wrong.example.com"
    elif failure == "audience":
        state["claims"]["aud"] = "wrong"
    elif failure == "signature":
        state["key"] = other
    else:
        state["claims"]["exp"] = int(time.time()) - 60
    assert finish(value).status_code == 401
    assert client.get("/private").status_code == 401
    assert finish(value).status_code == 400
