from datetime import timedelta
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select
from plumbline.auth.guard import install_guard
from plumbline.auth.models import ApiToken, AuditEvent, SessionRecord, User
from plumbline.auth.router import create_router
from plumbline.auth.service import bootstrap, now
from plumbline.persistence.baseline import upgrade as baseline
from plumbline.persistence.models import init_engine_and_tables, session_for
from plumbline.persistence.revision_0003_auth import upgrade


def test_lifecycle(tmp_path):
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    engine = init_engine_and_tables(url)
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        baseline(op)
        upgrade(op)
    with session_for(url) as db:
        admin = bootstrap(db, "admin", "initial-password-123")
        admin_id = admin.id
    def get_db():
        with session_for(url) as db:
            yield db
    app = FastAPI()
    app.include_router(create_router(get_db, secure_cookie=True))
    @app.get("/private")
    def private():
        return {"ok": True}
    @app.post("/private")
    def mutate():
        return {"ok": True}
    install_guard(app, url)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/private").status_code == 401
    assert client.post("/auth/login", json={"username": "admin", "password": "initial-password-123"}).status_code == 403
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    for _ in range(5):
        assert client.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "missing", "password": "bad"}).status_code == 401
    assert client.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "missing", "password": "bad"}).status_code == 429
    login = client.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "admin", "password": "initial-password-123"})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"] and "Secure" in login.headers["set-cookie"]
    csrf = login.json()["csrf_token"]
    headers = {"x-csrf-token": csrf}
    assert client.get("/private").status_code == 403
    assert client.post("/auth/password", json={"current_password": "initial-password-123", "new_password": "replacement-password-123"}).status_code == 403
    assert client.post("/auth/password", headers=headers, json={"current_password": "initial-password-123", "new_password": "replacement-password-123"}).status_code == 200
    assert client.get("/private").status_code == 401
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    login = client.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "admin", "password": "replacement-password-123"})
    headers = {"x-csrf-token": login.json()["csrf_token"]}
    assert client.get("/private").status_code == 200
    assert client.post("/private").status_code == 403
    assert client.patch(f"/auth/users/{admin_id}", headers=headers, json={"active": False, "role": "viewer"}).status_code == 409
    token = client.post("/auth/tokens", headers=headers, json={"role": "viewer", "expires_days": 1}).json()
    bearer = {"Authorization": "Bearer " + token["token"]}
    assert client.get("/private", headers=bearer).status_code == 200
    assert client.post("/private", headers=bearer).status_code == 403
    assert client.delete(f"/auth/tokens/{token['id']}", headers=headers).status_code == 200
    assert client.get("/private", headers=bearer).status_code == 401
    created = client.post("/auth/users", headers=headers, json={"username": "viewer", "password": "viewer-password-123", "role": "viewer"})
    assert created.status_code == 201
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/private").status_code == 401
    with session_for(url) as db:
        assert not db.exec(select(SessionRecord)).all()
        assert not db.exec(select(ApiToken)).all()
        audit = str(db.exec(select(AuditEvent)).all())
        assert "password" not in audit.replace("password_changed", "")
        assert token["token"] not in audit
        user = db.get(User, created.json()["id"])
        user.force_change = False
        db.add(user)
        db.commit()
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    login = client.post("/auth/login", headers={"x-csrf-token": csrf}, json={"username": "viewer", "password": "viewer-password-123"})
    viewer_headers = {"x-csrf-token": login.json()["csrf_token"]}
    assert client.get("/private").status_code == 200
    assert client.get("/auth/users").status_code == 403
    assert client.post("/private", headers=viewer_headers).status_code == 403
    assert client.post("/auth/tokens", headers=viewer_headers, json={"role": "admin", "expires_days": 1}).status_code == 403
    viewer_token = client.post("/auth/tokens", headers=viewer_headers, json={"role": "viewer", "expires_days": 1}).json()
    with session_for(url) as db:
        record = db.get(ApiToken, viewer_token["id"])
        record.expires_at = now() - timedelta(seconds=1)
        db.add(record)
        user = db.get(User, created.json()["id"])
        user.active = False
        db.add(user)
        db.commit()
    assert client.get("/private").status_code == 401
    assert client.get("/private", headers={"Authorization": "Bearer " + viewer_token["token"]}).status_code == 401
