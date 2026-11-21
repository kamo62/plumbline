import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from datetime import datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from plumbline import runner
from plumbline.api.models import CollectionConfig
from plumbline.config.settings import Settings


class FakeClient:
    def __init__(self, settings):
        self.org = settings.azure_devops_org
        self.closed = False

    async def close(self):
        self.closed = True


def settings(database_url=None):
    return Settings(azure_devops_pat="test-pat", azure_devops_org="test-org",
                    database_url=database_url, exemptions_file=None)


def config(tmp_path, **kwargs):
    values = dict(project="test-project", write_files=False, write_db=False,
                  include_maturity=False, output_path=str(tmp_path / "metrics"))
    values.update(kwargs)
    return CollectionConfig(**values)


def fake_services(monkeypatch, *, fail=False):
    clients, options = [], []

    def client_factory(settings):
        client = FakeClient(settings)
        clients.append(client)
        return client

    async def collect(projects, window):
        options.append((projects, window))
        if fail:
            raise RuntimeError("collection failed")
        return {"project": projects[0], "repositories": [{"id": "repo-1"}, {"id": "repo-2"}]}

    from tests.provider_stub import inject
    inject(monkeypatch, collect)
    monkeypatch.setattr(runner, "AzureDevOpsClient", client_factory)
    return clients, options


@pytest.mark.asyncio
async def test_database_save_error_does_not_expose_sql(tmp_path, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError
    fake_services(monkeypatch)

    def fail_save(*args):
        raise OperationalError("INSERT private_statement", {"private_parameter": "sensitive"},
                               RuntimeError("database is locked"))

    result = await runner.execute_collection_run(config(tmp_path), settings(), persist_result=fail_save)
    assert result["status"] == "failed"
    assert result["error"] == "Database operation failed"
    assert "private_statement" not in caplog.text
    assert "private_parameter" not in caplog.text


@pytest.mark.asyncio
async def test_synthetic_collection_uses_full_window_and_actual_files(tmp_path, monkeypatch):
    clients, options = fake_services(monkeypatch)
    # Exercise the actual FileSink fallback using data the Excel exporter cannot consume.
    run_id = str(uuid4())
    stale_excel = tmp_path / run_id / "metrics.xlsx"
    stale_excel.parent.mkdir()
    stale_excel.write_text("stale artifact from an earlier attempt")
    result = await runner.execute_collection_run(config(tmp_path, org="override", write_files=True), settings(), run_id)
    assert result["status"] == "succeeded"
    assert clients[0].org == "override" and clients[0].closed
    assert options[0][0] == ["test-project"]
    assert options[0][1].start == datetime(options[0][1].end.year, 1, 1, tzinfo=timezone.utc)
    raw_path = Path(result["outputs"]["raw_json"])
    assert raw_path.parent == tmp_path / run_id
    assert len(json.loads(raw_path.read_text())["repositories"]) == 2
    assert all(Path(path).is_file() for path in result["outputs"].values())
    assert not (tmp_path / "metrics.json").exists()
    assert "raw_excel" not in result["outputs"]


@pytest.mark.asyncio
async def test_failure_closes_client_and_strict_db_does_not_create_client(tmp_path, monkeypatch):
    clients, _ = fake_services(monkeypatch, fail=True)
    result = await runner.execute_collection_run(config(tmp_path), settings())
    assert result["status"] == "failed" and result["error"] == "collection failed"
    assert clients[0].closed
    monkeypatch.setenv("PLUMBLINE_DATABASE_URL", "")
    result = await runner.execute_collection_run(config(tmp_path, write_db=True, db_strict=True), settings())
    assert result["status"] == "failed" and "PLUMBLINE_DATABASE_URL" in result["error"]
    assert len(clients) == 1


@pytest.mark.parametrize("removed", ["include_segments", "minimal", "fast", "full", "db_reset", "backfill_from", "validate_calculations", "dry_run", "continue_on_error", "max_errors", "auto_context"])
def test_removed_config_options_are_rejected(removed):
    with pytest.raises(ValidationError):
        CollectionConfig(project="test-project", **{removed: False})


@pytest.mark.asyncio
async def test_shared_engine_is_not_disposed_and_metadata_updated(tmp_path, monkeypatch):
    fake_services(monkeypatch)
    statements = []
    class Connection:
        def execute(self, statement, params):
            statements.append((str(statement), params))
    class Transaction:
        def __enter__(self): return Connection()
        def __exit__(self, *args): pass
    class Engine:
        disposed = False
        def begin(self): return Transaction()
        def dispose(self): self.disposed = True
    engine = Engine()
    monkeypatch.setattr(runner, "init_engine_and_tables", lambda *a, **k: engine)
    monkeypatch.setattr(runner, "PostgresSink", lambda engine: SimpleNamespace(persist=lambda *args: None))
    result = await runner.execute_collection_run(config(tmp_path, org="override", write_db=True), settings("postgresql://unused"))
    assert result["status"] == "succeeded" and not engine.disposed
    statement, params = statements[0]
    assert "ON CONFLICT (id) DO NOTHING" in statement
    assert params["org"] == "override" and params["project"] == "test-project"


@pytest.mark.asyncio
async def test_incomplete_collection_keeps_raw_evidence_and_blocks_scoring(tmp_path, monkeypatch):
    fake_services(monkeypatch)
    calls = []
    async def collect(projects, window):
        calls.append(projects)
        return {"repositories": [{"id": "good"}], "completeness": {
            "complete": False, "failures": [{"scope": "build", "build_id": 7, "errors": {"tests": "unavailable"}}]}}
    from tests.provider_stub import inject
    inject(monkeypatch, collect)
    monkeypatch.setattr(runner.MetricsServiceFactory, "create_service", lambda *a, **k: pytest.fail("incomplete collection assessed"))
    result = await runner.execute_collection_run(config(tmp_path, include_maturity=True, write_files=True), settings())
    assert result["status"] == "succeeded"
    assert result["assessment_status"] == "blocked_incomplete_collection"
    assert result["completeness"]["failures"][0]["build_id"] == 7
    assert calls == [["test-project"]]
    assert json.loads(Path(result["outputs"]["raw_json"]).read_text())["repositories"] == [{"id": "good"}]
    assert "maturity_assessment" not in result["outputs"]


@pytest.mark.parametrize("selection", [
    {}, {"project": " "}, {"projects": []}, {"projects": ["ok", " "]},
    {"project": "one", "projects": ["two"]},
    {"projects": ["one"], "all_projects": True},
    {"project": "one", "all_projects": True},
])
def test_invalid_project_selections(selection):
    with pytest.raises(ValidationError):
        CollectionConfig(**selection)


def test_duplicate_project_selection_is_normalised():
    assert CollectionConfig(projects=[" one ", "two", "one"]).projects == ["one", "two"]


class SyntheticTransport(FakeClient):
    def __init__(self, settings, collision=False):
        super().__init__(settings)
        self.collision = collision
        self.project_calls = []

    async def get_projects(self):
        return [{"id": name, "name": name, "state": "wellFormed"} for name in ("Archive Alpha", "Beta", "Broken")]

    async def get_project(self, project):
        self.project_calls.append(project)
        if project in ("Broken", "Missing"):
            raise ValueError("synthetic project failure")
        return {"id": project, "name": project}

    async def get_repositories(self, project):
        return [{"id": project, "name": project, "project": {"id": project, "name": project, "organisation": "synthetic-org"}}]

    async def get_commits(self, *args): return []
    async def get_branches(self, *args): return []
    async def get_pull_requests(self, *args): return []
    async def get_policy_configurations(self, *args): return []
    async def get_policy_types(self, *args): return []
    async def get_builds(self, project, *args):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        return [{"id": 1 if self.collision or project == "Archive Alpha" else 2, "queueTime": stamp, "finishTime": stamp, "status": "completed", "result": "succeeded", "repository": {"id": project}}]
    async def get_code_coverage(self, *args): return {}
    async def get_test_results(self, *args): return {"results": [], "unlinked_runs": 0}
    async def get_security_scan_results(self, *args): return []


@pytest.mark.asyncio
async def test_selected_projects_real_collector_blocks_one_consolidated_run(tmp_path, monkeypatch):
    clients, assessments = [], []
    def client_factory(settings):
        client = SyntheticTransport(settings)
        clients.append(client)
        return client
    def create(service, client, **kwargs):
        assessments.append(service)
        pytest.fail("incomplete evidence must not be assessed")
    monkeypatch.setattr(runner, "AzureDevOpsClient", client_factory)
    monkeypatch.setattr(runner.MetricsServiceFactory, "create_service", create)
    result = await runner.execute_collection_run(CollectionConfig(
        projects=["Archive Alpha", "Beta", "Broken", "Missing", "Beta"],
        write_files=True, write_db=False, include_maturity=True,
        output_path=str(tmp_path / "metrics")), settings())
    assert result["status"] == "succeeded"
    assert str(UUID(result["id"])) == result["id"]
    assert result["assessment_status"] == "blocked_incomplete_collection"
    assert {f["project_id"] for f in result["completeness"]["failures"]} == {"Broken", "Missing"}
    raw = json.loads(Path(result["outputs"]["raw_json"]).read_text())
    assert {repo["id"] for repo in raw["repositories"]} == {"Archive Alpha", "Beta"}
    assert all(repo["project"]["organisation"] == "synthetic-org" for repo in raw["repositories"])
    assert all(data["project"]["organisation"] == "synthetic-org" for data in raw["repository_data"].values())
    assert len(raw["builds"]) == 2 and len(raw["build_data"]) == 2
    assert clients[0].project_calls == ["Archive Alpha", "Beta", "Broken", "Missing"]
    assert clients[0].closed and not assessments


@pytest.mark.asyncio
async def test_build_id_collision_marks_consolidation_incomplete():
    from plumbline.backend.services.raw_data_collector import RawDataCollector
    client = SyntheticTransport(settings(), collision=True)
    raw = await RawDataCollector(client).collect_all_projects_raw_data(["Archive Alpha", "Beta"])
    summary = runner.collection_summary(raw)
    assert not summary["complete"]
    assert summary["failures"][0]["build_ids"] == ["1"]
    assert len(raw["build_data"]) == 1 and len(raw["builds"]) == 2


@pytest.mark.asyncio
async def test_two_projects_create_one_consolidated_assessment(tmp_path, monkeypatch):
    captured = []
    class Assessment:
        def assess_raw_data(self, raw):
            captured.append(raw)
            return {"repositories": {}}
    monkeypatch.setattr(runner, "AzureDevOpsClient", SyntheticTransport)
    monkeypatch.setattr(runner.MetricsServiceFactory, "create_service",
                        lambda service, client, **kwargs: Assessment())
    result = await runner.execute_collection_run(CollectionConfig(
        projects=["Archive Alpha", "Beta"], write_files=False, write_db=False,
        include_maturity=True, output_path=str(tmp_path / "metrics")), settings())
    assert result["assessment_status"] == "completed" and result["completeness"]["complete"]
    assert len(captured) == 1
    assert {repo["id"] for repo in captured[0]["repositories"]} == {"Archive Alpha", "Beta"}


@pytest.mark.asyncio
async def test_configured_missing_or_invalid_exemptions_fail_before_collection(tmp_path):
    for path in (tmp_path / 'missing.yml', tmp_path / 'invalid.yml'):
        if path.name == 'invalid.yml':
            path.write_text('exemptions: [invalid]')
        configured = settings().model_copy(update={'exemptions_file': path})
        result = await runner.execute_collection_run(config(tmp_path), configured)
        assert result['status'] == 'failed'
        assert 'Invalid exemptions configuration' in result['error']


@pytest.mark.asyncio
async def test_failure_shaped_assessment_is_not_reported_completed(tmp_path, monkeypatch):
    fake_services(monkeypatch)
    class FailedAssessment:
        def assess_raw_data(self, raw):
            return {'repositories': {}, 'overall': {'explanation': ['Assessment failed']}}
    monkeypatch.setattr(runner.MetricsServiceFactory, 'create_service', lambda *args, **kwargs: FailedAssessment())
    result = await runner.execute_collection_run(config(tmp_path, include_maturity=True, write_files=True), settings())
    assert result['assessment_status'] == 'failed'
    assert result['assessment_error']['code'] == 'assessment_failed'
    assert result['outputs'].get('raw_json')
    assert not list(tmp_path.rglob('*maturity*'))
