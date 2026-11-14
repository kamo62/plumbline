from typer.testing import CliRunner

from plumbline.cli import app


runner = CliRunner()


def test_cli_help_lists_collection_command():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "collect-raw" in result.stdout


def test_cli_rejects_unknown_collection_option():
    result = runner.invoke(app, ["collect-raw", "--not-a-real-option"])

    assert result.exit_code == 2
    assert "No such option" in result.output


def test_cli_delegates_equivalent_config_and_preserves_failure_exit(monkeypatch):
    from plumbline import cli
    from plumbline.api.models import CollectionConfig
    captured = []
    async def execute(config, settings):
        captured.append(config)
        return {"id": "synthetic", "status": "failed", "error": "collection failed"}
    monkeypatch.setenv("PLUMBLINE_AZURE_DEVOPS_PAT", "test-pat")
    monkeypatch.setenv("PLUMBLINE_AZURE_DEVOPS_ORG", "test-org")
    monkeypatch.setattr(cli, "execute_collection_run", execute)
    result = runner.invoke(app, ["collect-raw", "--project", "test-project", "--maturity", "--no-write-files"])
    assert result.exit_code == 1
    assert captured == [CollectionConfig(project="test-project", include_maturity=True, write_files=False, write_db=False)]


def test_removed_cli_options_are_not_exposed():
    for option in ("--segments", "--minimal", "--fast", "--full", "--db-reset", "--backfill-from", "--validate-calculations"):
        result = runner.invoke(app, ["collect-raw", option])
        assert result.exit_code == 2
        assert "No such option" in result.output


def test_cli_selected_projects_share_one_run_and_expose_partial_outcome(monkeypatch):
    from plumbline import cli
    captured = []
    async def execute(config, settings):
        captured.append(config)
        return {"id": "synthetic", "status": "succeeded", "completeness": {
            "complete": False, "failures": [{"scope": "project", "error": "unavailable"}]}}
    monkeypatch.setenv("PLUMBLINE_AZURE_DEVOPS_PAT", "test-pat")
    monkeypatch.setenv("PLUMBLINE_AZURE_DEVOPS_ORG", "test-org")
    monkeypatch.setattr(cli, "execute_collection_run", execute)
    result = runner.invoke(app, ["collect-raw", "--projects", "First", "--projects", "Second", "--projects", "First"])
    assert result.exit_code == 1
    assert len(captured) == 1 and captured[0].projects == ["First", "Second"]
    assert "Collection incomplete" in result.output and "unavailable" in result.output
