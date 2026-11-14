import os
import subprocess
from pathlib import Path


def test_runner_isolates_environment_and_propagates_failure(tmp_path):
    root = Path(__file__).resolve().parents[1]
    python = tmp_path / "fake-python"
    result_file = tmp_path / "result"
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PLUMBLINE_AZURE_DEVOPS_PAT\" \"$PLUMBLINE_DATABASE_URL\" "
        "\"${PRIVATE_SENTINEL-unset}\" \"$(test -e .env && echo present || echo absent)\" "
        f"\"$*\" > '{result_file}'\n"
        "exit 37\n"
    )
    python.chmod(0o755)
    env = os.environ | {"PYTHON": str(python), "PRIVATE_SENTINEL": "secret"}
    result = subprocess.run([str(root / "scripts/test-all.sh")], cwd=root, env=env, check=False)
    assert result.returncode == 37
    lines = result_file.read_text().splitlines()
    assert lines[:4] == ["test-pat", "", "unset", "absent"]
    assert "--doctest-modules" in lines[4]
    assert str(root / "tests") in lines[4]
