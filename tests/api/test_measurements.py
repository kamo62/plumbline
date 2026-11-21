import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from plumbline.auth.guard import install_guard
from plumbline.auth.models import ApiToken, AuditEvent, SessionRecord, User
from plumbline.auth.service import digest, now
from plumbline.collection.store import snapshots
from plumbline.measurements import router, store
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import Run, init_engine_and_tables, session_for
from plumbline.persistence.sinks import PostgresSink
from plumbline.providers.ado import ADOProvider
from plumbline.providers.contract import Window


POLICY = "ado-reported-total/v1"
BODY = {"minimum_sample": 1, "population_policy": POLICY}
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 2, 1, tzinfo=timezone.utc)
AS_OF = datetime(2026, 2, 2, tzinfo=timezone.utc)


def make_snapshot():
    """Collect fictional inline ADO responses through a fully mocked client."""
    class Client:
        pass

    client = Client()
    build = {"id": 7, "repository": {"id": "repo-a"}, "status": "completed",
             "result": "succeeded", "finishTime": "2026-01-15T00:00:00Z"}
    responses = {
        "get_project": {"id": "project-a", "name": "private-source-payload"},
        "get_repositories": [{"id": "repo-a", "name": "Repository"}],
        "get_builds": [build],
        "get_policy_configurations": [], "get_policy_types": [],
        "get_commits": [], "get_branches": [], "get_pull_requests": [],
        "get_code_coverage": {}, "get_security_scan_results": [],
        "get_test_results": {"results": [{"total": 10, "passed": 8, "failed": 1,
                                          "skipped": 1, "not_executed": 0}],
                             "linked_runs": 1, "unlinked_runs": 0},
    }
    for name, response in responses.items():
        setattr(client, name, AsyncMock(return_value=response))
    snapshot = asyncio.run(ADOProvider(client, "connection-a", clock=lambda: AS_OF).collect(
        ["Project"], Window(START, END, AS_OF)))
    # Any later call is a test failure: measurement operates on the stored snapshot.
    for name in responses:
        getattr(client, name).side_effect = AssertionError("unexpected collection")
    return snapshot


@pytest.fixture
def application(tmp_path):
    url = f"sqlite:///{tmp_path / 'measurement-test.db'}"
    migrate(url)
    engine = init_engine_and_tables(url)
    admin_id, viewer_id, forced_id = (str(uuid4()) for _ in range(3))
    with session_for(url) as db:
        for user_id, role, forced in ((admin_id, "admin", False), (viewer_id, "viewer", False),
                                      (forced_id, "admin", True)):
            db.add(User(id=user_id, username=user_id, password_hash="unused", role=role,
                        active=True, force_change=forced))
        db.flush()
        for token, user_id, role in (("admin-token", admin_id, "admin"),
                                     ("viewer-token", viewer_id, "viewer"),
                                     ("forced-token", forced_id, "admin")):
            db.add(ApiToken(id=str(uuid4()), token_hash=digest(token), user_id=user_id,
                            role=role, expires_at=now() + timedelta(days=1)))
        db.add(SessionRecord(token_hash=digest("admin-session"), user_id=admin_id,
                             csrf_hash=digest("csrf-value"), expires_at=now() + timedelta(days=1)))
        db.commit()

    def get_db():
        with session_for(url) as db:
            yield db

    app = FastAPI()
    app.include_router(router.create_router(get_db))
    install_guard(app, url)
    with TestClient(app) as client:
        yield client, engine, admin_id


def bearer(role):
    return {"authorization": f"Bearer {role}-token"}


def saved_run(engine, snapshot=None):
    run_id = str(uuid4())
    sink = PostgresSink(engine)
    sink.persist({"provider_snapshot": snapshot or make_snapshot()}, {"run_id": run_id})
    return run_id, sink.last_snapshot_id


def path(run_id):
    return f"/v1/runs/{run_id}/measurements/test-pass-rate"


def test_guard_roles_force_change_and_cookie_csrf(application):
    client, _, _ = application
    run_id = str(uuid4())
    routes = [("post", path(run_id)), ("get", f"/v1/runs/{run_id}/measurements"),
              ("get", f"/v1/measurements/{'a' * 64}"),
              ("get", f"/v1/measurements/{'a' * 64}/export.json"),
              ("get", f"/v1/measurements/{'a' * 64}/verification")]
    for method, url in routes:
        assert client.request(method, url, json=BODY if method == "post" else None).status_code == 401
        assert client.request(method, url, headers=bearer("forced"),
                              json=BODY if method == "post" else None).status_code == 403
    assert client.post(path(run_id), headers=bearer("viewer"), json=BODY).status_code == 403
    client.cookies.set("plumbline_session", "admin-session")
    assert client.post(path(run_id), json=BODY).status_code == 403
    assert client.post(path(run_id), headers={"x-csrf-token": "wrong"}, json=BODY).status_code == 403
    assert client.post(path(run_id), headers={"x-csrf-token": "csrf-value"}, json=BODY).status_code == 404


@pytest.mark.parametrize("body", [
    {}, {"minimum_sample": 1}, {"population_policy": POLICY},
    {"minimum_sample": 0, "population_policy": POLICY},
    {"minimum_sample": -1, "population_policy": POLICY},
    {"minimum_sample": True, "population_policy": POLICY},
    {"minimum_sample": 1.0, "population_policy": POLICY},
    {"minimum_sample": "1", "population_policy": POLICY},
    {"minimum_sample": 1, "population_policy": "other"},
    {**BODY, "extra": True},
])
def test_invalid_request_body_is_rejected(application, body):
    client, _, _ = application
    assert client.post(path(uuid4()), headers=bearer("admin"), json=body).status_code == 422


def test_invalid_identifiers_and_missing_records(application):
    client, _, _ = application
    assert client.post(path("not-a-uuid"), headers=bearer("admin"), json=BODY).status_code == 422
    assert client.get("/v1/runs/not-a-uuid/measurements", headers=bearer("viewer")).status_code == 422
    assert client.get(f"/v1/runs/{uuid4()}/measurements", headers=bearer("viewer")).status_code == 404
    assert client.post(path(uuid4()), headers=bearer("admin"), json=BODY).status_code == 404
    for suffix in ("", "/export.json", "/verification"):
        assert client.get(f"/v1/measurements/{'a' * 64}{suffix}", headers=bearer("viewer")).status_code == 404
        for bad_id in ("A" * 64, "a" * 63, "z" * 64, "a" * 64 + "%0A"):
            assert client.get(f"/v1/measurements/{bad_id}{suffix}", headers=bearer("viewer")).status_code == 422


def test_original_snapshot_to_frozen_record_export_and_verification(application):
    client, engine, admin_id = application
    run_id, snapshot_id = saved_run(engine)
    created = client.post(path(run_id.upper()), headers=bearer("admin"), json=BODY)
    assert created.status_code == 200, created.text
    record = created.json()
    assert len(record["id"]) == 64 and all(c in "0123456789abcdef" for c in record["id"])
    assert record["run_id"] == run_id and record["snapshot_id"] == snapshot_id
    assert record["document"]["snapshot_id"] == snapshot_id
    assert record["document"]["minimum_sample"] == 1
    assert record["document"]["population_policy"] == POLICY
    result, = record["document"]["results"]
    assert result["state"] == "measured"
    assert (result["numerator"], result["denominator"], result["value"]) == (8, 10, 80.0)
    assert result["coverage"]["observed"] == result["coverage"]["expected"] == 1
    assert result["scope"] == {"connection": "connection-a", "project": "project-a",
                               "repository": "repo-a", "build": None}
    assert "private-source-payload" not in json.dumps(record)
    measurement_url = f"/v1/measurements/{record['id']}"
    assert client.get(measurement_url, headers=bearer("viewer")).json() == record
    exported = client.get(measurement_url + "/export.json", headers=bearer("viewer"))
    assert exported.status_code == 200 and exported.json() == record
    assert exported.headers["content-type"].startswith("application/json")
    assert exported.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/v1/runs/{run_id}/measurements", headers=bearer("viewer")).json() == [record]
    with engine.connect() as conn:
        expected_verification = store.verify_measurement(conn, record["id"])
    verification = client.get(measurement_url + "/verification", headers=bearer("viewer"))
    assert verification.status_code == 200 and verification.json() == expected_verification
    repeated = client.post(path(run_id), headers=bearer("admin"), json=BODY)
    assert repeated.status_code == 200 and repeated.json() == record
    with engine.connect() as conn:
        events = conn.execute(sa.select(AuditEvent.__table__).where(
            AuditEvent.action == "metric_measurement_requested")).mappings().all()
    assert len(events) == 2
    assert all(e["actor_id"] == admin_id and e["subject_id"] == record["id"] for e in events)


def test_missing_or_corrupt_snapshot_is_a_safe_conflict(application):
    client, engine, _ = application
    unlinked = uuid4()
    with engine.begin() as conn:
        conn.execute(sa.insert(Run.__table__).values(id=unlinked, executed_at=AS_OF))
    assert client.post(path(unlinked), headers=bearer("admin"), json=BODY).status_code == 409
    run_id, snapshot_id = saved_run(engine)
    created = client.post(path(run_id), headers=bearer("admin"), json=BODY)
    assert created.status_code == 200
    record = created.json()
    with engine.begin() as conn:
        payload = conn.scalar(sa.select(snapshots.c.payload).where(snapshots.c.id == snapshot_id))
        payload["projects"][0]["fields"]["project"]["detail"] = "sensitive-corruption"
        conn.execute(sa.update(snapshots).where(snapshots.c.id == snapshot_id).values(payload=payload))
    response = client.post(path(run_id), headers=bearer("admin"), json=BODY)
    assert response.status_code == 409 and "sensitive-corruption" not in response.text
    measurement_url = f"/v1/measurements/{record['id']}"
    assert client.get(measurement_url, headers=bearer("viewer")).json() == record
    assert client.get(measurement_url + "/export.json", headers=bearer("viewer")).json() == record
    verification = client.get(measurement_url + "/verification", headers=bearer("viewer")).json()
    assert verification["snapshot_intact"] is False
    assert verification["reproducible"] is None
    with engine.connect() as conn:
        events = conn.execute(sa.select(AuditEvent.__table__).where(
            AuditEvent.action == "metric_measurement_requested")).all()
    assert len(events) == 1


def test_audit_failure_rolls_back_measurement(application, monkeypatch):
    client, engine, _ = application
    run_id, _ = saved_run(engine)
    original_event = router.event

    def broken_event(db, *args):
        original_event(db, *args)
        raise SQLAlchemyError("secret sql detail")

    monkeypatch.setattr(router, "event", broken_event)
    response = client.post(path(run_id), headers=bearer("admin"), json=BODY)
    assert response.status_code == 503 and "secret sql detail" not in response.text
    with engine.connect() as conn:
        assert store.list_measurements(conn, run_id) == []
        assert not conn.execute(sa.select(AuditEvent.__table__).where(
            AuditEvent.action == "metric_measurement_requested")).all()


@pytest.mark.parametrize("method,store_name,suffix", [
    ("post", "create_measurement", ""),
    ("get", "list_measurements", ""),
    ("get", "get_measurement", ""),
    ("get", "get_measurement", "/export.json"),
    ("get", "verify_measurement", "/verification"),
])
def test_database_errors_are_safe(application, monkeypatch, method, store_name, suffix):
    client, _, _ = application

    def broken(*args, **kwargs):
        raise SQLAlchemyError("sql and credential must not be echoed")

    monkeypatch.setattr(store, store_name, broken)
    if method == "post":
        url = path(uuid4())
    elif store_name == "list_measurements":
        url = f"/v1/runs/{uuid4()}/measurements"
    else:
        url = f"/v1/measurements/{'a' * 64}{suffix}"
    response = client.request(method, url, headers=bearer("admin"), json=BODY if method == "post" else None)
    assert response.status_code == 503
    assert "sql" not in response.text.lower() and "credential" not in response.text.lower()
