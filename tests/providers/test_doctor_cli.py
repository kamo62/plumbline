import json

import typer
from typer.testing import CliRunner

from plumbline.provider_cli import register


def test_synthetic_doctor_reports_failures_without_raw_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = typer.Typer()
    register(app)
    result = CliRunner().invoke(app, ['--provider', 'synthetic'])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert 'tests' in payload['capabilities']
    assert any(row[0] == 'tests' and row[1] == 'failed' for row in payload['checks'])
    assert 'raw' not in payload and 'Alpha' not in result.output
    assert list(tmp_path.iterdir()) == []


def test_doctor_rejects_unsupported_provider_and_naive_dates():
    app = typer.Typer()
    register(app)
    runner = CliRunner()
    assert runner.invoke(app, ['--provider', 'unknown']).exit_code == 2
    assert runner.invoke(app, ['--provider', 'synthetic', '--start', '2026-01-01']).exit_code == 2
