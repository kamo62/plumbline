from datetime import datetime, timedelta, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from plumbline.collection import collect_full_window, load_snapshot, persist_snapshot
from plumbline.collection.store import checkpoints, snapshots
from plumbline.persistence.revision_0005_evidence import upgrade
from plumbline.providers.ado import ADOProvider
from plumbline.providers.contract import EvidenceState, Window


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        upgrade(Operations(MigrationContext.configure(conn)))
    return engine


class Source:
    def __init__(self):
        self.late = False
        self.fail = False

    async def get_project(self, _): return {"id": "p", "name": "Project"}
    async def get_repositories(self, _): return [{"id": "r"}]
    async def get_builds(self, *_):
        return [{"id": 7, "queueTime": "2026-01-01T00:00:00Z", "repository": {"id": "r"}}]
    async def get_commits(self, *_): return [{"id": "boundary", "date": "2026-01-01T00:00:00Z"}]
    async def get_branches(self, *_): return []
    async def get_branch_policies(self, *_): return []
    async def get_pull_requests(self, *_):
        pr = {"pullRequestId": 1, "creationDate": "2025-12-01T00:00:00Z"}
        if self.late:
            pr["closedDate"] = "2026-01-15T00:00:00Z"
        return [pr]
    async def get_test_results(self, *_):
        if self.fail:
            raise RuntimeError("temporary")
        return {"results": [{"total": 5, "passed": 4}]} if self.late else {"results": []}
    async def get_code_coverage(self, *_): return {"line": 75} if self.late else {}
    async def get_security_scan_results(self, *_): return []


@pytest.mark.asyncio
async def test_full_fallback_refreshes_and_preserves_history(engine):
    source = Source()
    asof = datetime(2026, 2, 2, tzinfo=timezone.utc)
    window = Window(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc), asof)
    provider = ADOProvider(source, "connection", clock=lambda: asof)
    initial = await collect_full_window(provider, ["Project"], window)
    with engine.begin() as conn:
        original_id = persist_snapshot(conn, initial)
        assert persist_snapshot(conn, initial) == original_id
        assert conn.scalar(sa.select(sa.func.count()).select_from(snapshots)) == 1
    source.fail = True
    source.late = True
    failed = await collect_full_window(provider, ["Project"], window)
    assert failed.builds[0].fields["tests"].state == EvidenceState.FAILED
    with engine.begin() as conn:
        persist_snapshot(conn, failed)
    source.fail = False
    refreshed = await collect_full_window(provider, ["Project"], window)
    full = await provider.collect(["Project"], window)
    assert refreshed == full
    assert refreshed.repositories[0].fields["commits"].value[0]["id"] == "boundary"
    assert refreshed.repositories[0].fields["pull_requests"].value[0]["closedDate"] == "2026-01-15T00:00:00Z"
    assert refreshed.builds[0].fields["tests"].value["results"][0]["passed"] == 4
    assert refreshed.builds[0].fields["coverage"].value["line"] == 75
    with engine.begin() as conn:
        new_id = persist_snapshot(conn, refreshed)
        assert load_snapshot(conn, new_id) == refreshed
        assert load_snapshot(conn, original_id) == initial
        assert new_id != original_id


@pytest.mark.asyncio
async def test_checkpoint_failure_and_rollback(engine):
    source = Source()
    asof = datetime(2026, 2, 2, tzinfo=timezone.utc)
    provider = ADOProvider(source, "c", clock=lambda: asof)
    w = Window(asof - timedelta(days=32), asof - timedelta(days=1), asof)
    source.fail = True
    partial = await provider.collect(["Project"], w)
    with engine.begin() as conn:
        persist_snapshot(conn, partial)
        rows = conn.execute(sa.select(checkpoints)).mappings().all()
        assert any(row["field"] == "coverage" for row in rows)
        assert not any(row["field"] == "tests" for row in rows)
    source.fail = False
    complete = await provider.collect(["Project"], w)
    with pytest.raises(RuntimeError), engine.begin() as conn:
        persist_snapshot(conn, complete)
        raise RuntimeError("rollback")
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(checkpoints).where(checkpoints.c.field == "tests")) == 0
    with engine.begin() as conn:
        persist_snapshot(conn, complete)
        assert conn.scalar(sa.select(sa.func.count()).select_from(checkpoints).where(checkpoints.c.field == "tests")) == 1


def test_postgres_migration_ddl_compiles_offline():
    ddl = []
    engine = sa.create_mock_engine("postgresql://", lambda statement, *a, **kw: ddl.append(str(statement.compile(dialect=postgresql.dialect()))))
    upgrade(Operations(MigrationContext.configure(engine.connect())))
    assert any("CREATE TABLE evidence_snapshots" in statement for statement in ddl)
    assert any("CREATE TABLE evidence_checkpoints" in statement for statement in ddl)
