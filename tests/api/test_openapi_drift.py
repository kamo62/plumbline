"""Keep the checked-in API schema aligned without startup or database access."""
import json
from pathlib import Path

from plumbline.api.main import app


def test_checked_in_openapi_matches_application():
    path = Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'openapi.json'
    assert json.loads(path.read_text()) == app.openapi(), 'Run frontend generate:api'
