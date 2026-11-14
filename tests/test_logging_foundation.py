import json
import logging
import os
import subprocess
import sys

from plumbline.utils.logging import get_logger


def test_child_logger_propagates_to_caplog(caplog):
    logger = get_logger("plumbline.test.foundation")
    with caplog.at_level(logging.INFO):
        logger.info("captured message")
    assert "captured message" in caplog.text
    assert not logger.handlers


def test_entry_configuration_is_once_and_json_exceptions_use_stderr(tmp_path):
    code = '''\nfrom plumbline.utils.logging import configure_logging, get_logger\nconfigure_logging()\nconfigure_logging()\ntry:\n    raise ValueError("synthetic failure")\nexcept ValueError:\n    get_logger("plumbline.test").exception("failed")\n'''
    env = dict(os.environ, PLUMBLINE_LOG_JSON="true", PLUMBLINE_LOG_LEVEL="INFO")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            text=True, capture_output=True, check=True)
    assert result.stdout == ""
    records = result.stderr.splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["message"] == "failed"
    assert "ValueError: synthetic failure" in record["exception"]
    assert not (tmp_path / "logs").exists()
