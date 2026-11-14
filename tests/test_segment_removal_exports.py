"""Exercise current exports and comparisons using synthetic records."""
import subprocess
import sys
from pathlib import Path

import pandas as pd

from plumbline.persistence.sinks import CompositeSink, FileSink


def test_sink_and_comparison_without_segments(tmp_path):
    outputs = []
    for period, score, name in (("previous", 40, "first"), ("current", 65, "renamed")):
        results = {
            "file_output_base": str(tmp_path / period / "metrics"),
            "raw_ado_data": {"project": {"id": "p", "name": "Synthetic"}, "repositories": [{"id": "r1", "name": name}]},
            "maturity_data": {"repositories": {"r1": {"name": "repo", "project": {"name": "Synthetic"}, "overall": {"score": score}}}},
            "segment_scores": {"obsolete": {"maturity_score": 10}},
        }
        written = CompositeSink([FileSink()]).persist(results, {})
        assert {"raw_excel", "raw_json", "maturity_excel", "maturity_assessment"} <= written.keys()
        assert all(Path(path).is_file() for path in written.values())
        assert not list((tmp_path / period).glob("*segment*"))
        outputs.append(written)

    out = tmp_path / "comparison"
    script = Path(__file__).resolve().parents[1] / "scripts" / "compare_metrics.py"
    completed = subprocess.run([sys.executable, str(script), "--prev-raw", outputs[0]["raw_excel"], "--curr-raw", outputs[1]["raw_excel"], "--prev-maturity", outputs[0]["maturity_excel"], "--curr-maturity", outputs[1]["maturity_excel"], "--out-dir", str(out)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    raw = pd.read_csv(out / "raw_repository_changes.csv")
    maturity = pd.read_csv(out / "maturity_assessment_changes.csv")
    assert raw.loc[0, "row_status"] == "changed"
    assert raw.loc[0, "name__prev"] == "first" and raw.loc[0, "name__curr"] == "renamed"
    assert maturity.loc[0, "row_status"] == "changed"
    assert maturity.loc[0, "Overall Score__delta"] == 25
    assert (out / "summary.md").is_file()
