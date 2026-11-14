"""Generate the current schema without invoking application startup or a database."""
import json
import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'src'))
os.environ.update(PLUMBLINE_AZURE_DEVOPS_PAT='synthetic', PLUMBLINE_AZURE_DEVOPS_ORG='synthetic', PLUMBLINE_DATABASE_URL='', PLUMBLINE_RUN_ENV='ci')
with tempfile.TemporaryDirectory() as directory:
    os.chdir(directory)
    from plumbline.api.main import app
    (root / 'frontend/src/openapi.json').write_text(json.dumps(app.openapi(), indent=2) + '\n')
