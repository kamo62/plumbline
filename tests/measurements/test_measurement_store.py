import copy
import hashlib
from io import StringIO
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from plumbline.collection.store import checkpoints, link_run_snapshot, persist_snapshot, run_evidence, snapshots
from plumbline.persistence.migrations import HEAD, REVISIONS, migrate, status
from plumbline.persistence.models import Run, init_engine_and_tables
from plumbline.persistence.revision_0008_metric_measurements import upgrade
from plumbline.providers.contract import Evidence, EvidenceState, Identity, Record, Snapshot, Window


AS_OF = datetime(2026, 2, 2, tzinfo=timezone.utc)
WINDOW = Window(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc), AS_OF)
POLICY = "ado-reported-total/v1"


def evidence(value):
    return Evidence(value, EvidenceState.OBSERVED, "mocked", AS_OF, fresh_as_of=True)


def sample_snapshot():
    project = {"id": "project-1", "name": "Project"}
    repo = {"id": "repo-1", "name": "Repository"}
    build = {"id": 17, "status": "completed", "result": "succeeded",
             "finishTime": "2026-01-15T12:00:00Z", "repository": {"id": "repo-1"}}
    return Snapshot(
        "connection-1", WINDOW, frozenset({"projects", "repositories", "builds", "tests"}),
        projects=[Record(Identity("connection-1", "project-1"), {
            "project": evidence(project), "repositories": evidence([repo]), "builds": evidence([build]),
        })],
        repositories=[Record(Identity("connection-1", "project-1", "repo-1"), {"repository": evidence(repo)})],
        builds=[Record(Identity("connection-1", "project-1", build="17"), {
            "build": evidence(build), "tests": evidence({"results": [{"passed": 8, "total": 10}],
                                                        "linked_runs": 1, "unlinked_runs": 0}),
        })],
    )


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'measurement.db'}"
    migrate(url)
    return init_engine_and_tables(url)


@pytest.fixture
def store():
    from plumbline.measurements import store
    return store


def seed(conn):
    run_id = str(uuid4())
    conn.execute(sa.insert(Run.__table__).values(id=run_id, executed_at=AS_OF, status="succeeded"))
    snapshot_id = persist_snapshot(conn, sample_snapshot())
    link_run_snapshot(conn, run_id, snapshot_id)
    return run_id, snapshot_id


def create(store, conn, run_id, minimum=1):
    return store.create_measurement(conn, run_id, minimum_sample=minimum, population_policy=POLICY)


def replace_document(conn, store, record, edit):
    document = copy.deepcopy(record["document"])
    edit(document)
    document.pop("fingerprint")
    fingerprint = digest(document)
    document["fingerprint"] = fingerprint
    parameters_hash = digest({key: document[key] for key in store.adapter.PARAMETER_KEYS})
    conn.execute(sa.update(store.measurements).where(store.measurements.c.id == record["id"]).values(
        id=fingerprint, document=document, parameters_hash=parameters_hash,
    ))
    return fingerprint


def test_create_load_list_idempotence_and_independent_verification(engine, store, monkeypatch):
    with engine.begin() as conn:
        run_id, snapshot_id = seed(conn)
        record = create(store, conn, run_id.upper())
        assert record == create(store, conn, run_id)
        assert record["snapshot_id"] == snapshot_id
        assert record["run_id"] == run_id == record["document"]["run_id"]
        assert record["id"] == record["document"]["fingerprint"]
        assert digest({k: v for k, v in record["document"].items() if k != "fingerprint"}) == record["id"]
        assert store.list_measurements(conn, run_id) == [record]
        assert store.get_measurement(conn, record["id"]) == record
        verification = store.verify_measurement(conn, record["id"])
        assert verification["content_intact"] is verification["snapshot_intact"] is verification["reproducible"] is True
        assert verification["status"] == "verified" and verification["issues"] == []
        assert store.get_measurement(conn, "missing") is None
        assert store.verify_measurement(conn, "missing") is None
        monkeypatch.setattr(store.adapter, "build_test_pass_rate_document", lambda *a, **k: pytest.fail("recalculated"))
        assert store.get_measurement(conn, record["id"]) == record
        assert store.list_measurements(conn, run_id) == [record]


def test_conflict_and_outer_rollback(engine, store, monkeypatch):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        record = create(store, conn, run_id)
        builder = store.adapter.build_test_pass_rate_document

        def altered(*args, **kwargs):
            document = builder(*args, **kwargs)
            document["issues"] = ["different result"]
            return document

        monkeypatch.setattr(store.adapter, "build_test_pass_rate_document", altered)
        with pytest.raises(store.MeasurementError, match="conflicts"):
            create(store, conn, run_id)
        assert store.get_measurement(conn, record["id"]) == record
        monkeypatch.setattr(store.adapter, "build_test_pass_rate_document", builder)
    with pytest.raises(RuntimeError, match="rollback"), engine.begin() as conn:
        second = create(store, conn, run_id, minimum=2)
        assert store.get_measurement(conn, second["id"]) == second
        raise RuntimeError("rollback")
    with engine.connect() as conn:
        assert store.list_measurements(conn, run_id) == [record]


@pytest.mark.parametrize("part", ["results", "definition", "parameters_hash", "row_snapshot"])
def test_altered_records_are_rejected_but_verification_reports_content(engine, store, part):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        record = create(store, conn, run_id)
        if part in {"results", "definition"}:
            document = copy.deepcopy(record["document"])
            document[part] = "altered"
            values = {"document": document}
        elif part == "parameters_hash":
            values = {"parameters_hash": "0" * 64}
        else:
            # A different valid row snapshot exposes inconsistent row/document identities.
            other = sample_snapshot()
            other.connection = "other"
            other.projects = []
            other.repositories = []
            other.builds = []
            values = {"snapshot_id": persist_snapshot(conn, other)}
        conn.execute(sa.update(store.measurements).where(store.measurements.c.id == record["id"]).values(**values))
        with pytest.raises(store.MeasurementError, match="inconsistent"):
            store.get_measurement(conn, record["id"])
        with pytest.raises(store.MeasurementError, match="inconsistent"):
            store.list_measurements(conn, run_id)
        verified = store.verify_measurement(conn, record["id"])
        assert verified["content_intact"] is False and verified["reproducible"] is None
        assert "measurement_inconsistent" in verified["issues"]


@pytest.mark.parametrize("key", ["schema_version", "normaliser_version", "calculation_version", "catalogue_version", "definition"])
def test_consistent_unsupported_version_or_definition_never_calls_builder(engine, store, monkeypatch, key):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        record = create(store, conn, run_id)
        replacement_id = replace_document(conn, store, record, lambda doc: doc.update({key: "unknown"}))
        monkeypatch.setattr(store.adapter, "build_test_pass_rate_document", lambda *a, **k: pytest.fail("recalculated"))
        frozen = store.get_measurement(conn, replacement_id)
        assert frozen["document"][key] == "unknown"
        verification = store.verify_measurement(conn, replacement_id)
        assert verification["content_intact"] is verification["snapshot_intact"] is True
        assert verification["reproducible"] is None and verification["status"] == "unavailable"
        assert verification["issues"] == ["definition_or_policy_unavailable" if key == "definition" else "version_unavailable"]


def test_snapshot_non_scoring_tampering_is_separate_from_frozen_content(engine, store):
    with engine.begin() as conn:
        run_id, snapshot_id = seed(conn)
        record = create(store, conn, run_id)
        payload = conn.scalar(sa.select(snapshots.c.payload).where(snapshots.c.id == snapshot_id))
        payload["projects"][0]["fields"]["project"]["detail"] = "private non-scoring detail"
        conn.execute(sa.update(snapshots).where(snapshots.c.id == snapshot_id).values(payload=payload))
        assert store.get_measurement(conn, record["id"]) == record
        verification = store.verify_measurement(conn, record["id"])
        assert verification["content_intact"] is True and verification["snapshot_intact"] is False
        assert verification["reproducible"] is None and verification["issues"] == ["snapshot_inconsistent"]
        with pytest.raises(store.MeasurementError) as error:
            create(store, conn, run_id)
        assert "private" not in str(error.value)


def test_changed_builder_is_reported_as_reproduction_mismatch(engine, store, monkeypatch):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        record = create(store, conn, run_id)
        builder = store.adapter.build_test_pass_rate_document

        def altered(*args, **kwargs):
            document = builder(*args, **kwargs)
            document["issues"] = ["different result"]
            return document

        monkeypatch.setattr(store.adapter, "build_test_pass_rate_document", altered)
        verified = store.verify_measurement(conn, record["id"])
        assert verified["content_intact"] is verified["snapshot_intact"] is True
        assert verified["reproducible"] is False and verified["issues"] == ["reproduction_mismatch"]


def test_invalid_stored_json_is_reported_without_leaking_content(engine, store):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        record = create(store, conn, run_id)
        conn.execute(sa.text("UPDATE metric_measurements SET document = :raw WHERE id = :id"),
                     {"raw": "{private invalid", "id": record["id"]})
        with pytest.raises(store.MeasurementError) as error:
            store.get_measurement(conn, record["id"])
        assert "private" not in str(error.value)
        verified = store.verify_measurement(conn, record["id"])
        assert verified["content_intact"] is False and verified["snapshot_intact"] is True
        assert verified["issues"] == ["measurement_inconsistent"]


def test_measurement_foreign_keys_restrict_deletion(engine, store):
    with engine.begin() as conn:
        run_id, snapshot_id = seed(conn)
        create(store, conn, run_id)
        # Isolate the measurement's run restriction from the original link.
        conn.execute(sa.delete(run_evidence).where(run_evidence.c.run_id == run_id))
        conn.execute(sa.delete(checkpoints).where(checkpoints.c.snapshot_id == snapshot_id))
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(sa.delete(Run.__table__).where(Run.id == run_id))
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(sa.delete(snapshots).where(snapshots.c.id == snapshot_id))


def test_database_errors_are_not_reclassified_as_evidence_errors(engine, store, monkeypatch):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        record = create(store, conn, run_id)

        def unavailable(*args):
            raise sa.exc.OperationalError("query", {}, Exception("offline"))

        monkeypatch.setattr(store, "load_snapshot", unavailable)
        with pytest.raises(sa.exc.OperationalError):
            create(store, conn, run_id)
        with pytest.raises(sa.exc.OperationalError):
            store.verify_measurement(conn, record["id"])


def test_missing_run_and_missing_link_never_fall_back_to_job_result(engine, store):
    with engine.begin() as conn:
        with pytest.raises(store.RunNotFound):
            create(store, conn, str(uuid4()))
        with pytest.raises(store.RunNotFound):
            store.list_measurements(conn, str(uuid4()))
        run_id, _ = seed(conn)
        conn.execute(sa.delete(run_evidence).where(run_evidence.c.run_id == run_id))
        with pytest.raises(store.MeasurementError, match="snapshot"):
            create(store, conn, run_id)
        assert store.list_measurements(conn, run_id) == []


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1"])
def test_minimum_sample_is_explicit_positive_strict_integer(engine, store, value):
    with engine.begin() as conn:
        run_id, _ = seed(conn)
        with pytest.raises(ValueError):
            create(store, conn, run_id, minimum=value)


def test_additive_0007_upgrade_preserves_run_and_foreign_key_restrictions(tmp_path):
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    engine = init_engine_and_tables(url)
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        for _, apply, _ in REVISIONS[:7]:
            apply(op)
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('plumbline_0007')"))
        run_id, snapshot_id = seed(conn)
    assert status(url) == "plumbline_0007"
    assert migrate(url) == HEAD == "plumbline_0008"
    assert status(url) == HEAD
    with engine.connect() as conn:
        assert conn.scalar(sa.select(Run.id).where(Run.id == run_id)) is not None
        assert conn.scalar(sa.select(run_evidence.c.snapshot_id).where(run_evidence.c.run_id == run_id)) == snapshot_id
        fks = sa.inspect(conn).get_foreign_keys("metric_measurements")
        assert {fk["referred_table"] for fk in fks} == {"runs", "evidence_snapshots"}
        assert {fk["options"].get("ondelete") for fk in fks} == {"RESTRICT"}
        assert {tuple(c["column_names"]) for c in sa.inspect(conn).get_unique_constraints("metric_measurements")} == {
            ("run_id", "parameters_hash")
        }


def test_postgresql_measurement_ddl_compiles_without_connection():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    upgrade(Operations(context))
    ddl = output.getvalue()
    assert "CREATE TABLE metric_measurements" in ddl
    assert "REFERENCES runs (id) ON DELETE RESTRICT" in ddl
    assert "REFERENCES evidence_snapshots (id) ON DELETE RESTRICT" in ddl
    assert "UNIQUE (run_id, parameters_hash)" in ddl
    assert "ALTER COLUMN subject_id TYPE VARCHAR(64)" in ddl
