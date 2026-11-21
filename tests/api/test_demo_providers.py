import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from plumbline.api import demo
from plumbline.api import main as api
from plumbline.auth.models import ApiToken, User
from plumbline.auth.service import digest, now
from plumbline.config.settings import Settings
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import session_for


PACKS = Path(__file__).resolve().parents[2] / "synthetic_data"
ROUTE = "/v1/demo/providers"
VIEWER = {"Authorization": "Bearer synthetic-viewer-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    migrate(url)
    user_id = str(uuid4())
    with session_for(url) as db:
        db.add(User(id=user_id, username="synthetic_viewer", password_hash="unused",
                    role="viewer", active=True, force_change=False))
        db.flush()
        db.add(ApiToken(id=str(uuid4()), token_hash=digest("synthetic-viewer-token"),
                        user_id=user_id, role="viewer", expires_at=now() + timedelta(days=1)))
        db.commit()
    settings = Settings(database_url=url, exports_dir=tmp_path / "exports")
    monkeypatch.setitem(api.app.dependency_overrides, api.get_settings, lambda: settings)
    return TestClient(api.app)


def test_viewer_can_read_ordered_fictional_evidence(client, tmp_path, monkeypatch):
    inventory = [
        ("azure_devops.json", "Azure DevOps", [
            ("22222222-2222-4222-8222-222222222222", "synthetic_atlas"),
            ("44444444-4444-4444-8444-444444444444", "synthetic_empty"),
        ]),
        ("github.json", "GitHub", [
            ("900101", "synthetic_atlas"),
            ("900102", "synthetic_archived"),
        ]),
        ("gitlab.json", "GitLab", [
            ("900101", "synthetic_atlas"),
            ("900102", "synthetic_birch"),
        ]),
        ("bitbucket.json", "Bitbucket Cloud", [
            ("{22222222-2222-4222-8222-222222222222}", "synthetic_atlas"),
            ("{44444444-4444-4444-8444-444444444444}", "synthetic_empty"),
        ]),
    ]
    packs = {filename: (PACKS / filename).read_bytes() for filename, _, _ in inventory}

    # Neither the working directory nor a supplied path selects the data.
    monkeypatch.chdir(tmp_path)
    decoy = tmp_path / "synthetic_data" / "github.json"
    decoy.parent.mkdir()
    decoy.write_text("{}", encoding="utf-8")

    assert client.get(ROUTE).status_code == 401
    response = client.get(ROUTE, headers=VIEWER, params={"path": str(decoy)})
    assert response.status_code == 200, response.text
    expected = []
    for filename, label, repositories in inventory:
        pack = json.loads(packs[filename])
        expected.append({
            "id": pack["provider"],
            "label": label,
            "api_version": pack["api_version"],
            "window": pack["window"],
            "repositories": [{"id": ident, "name": name} for ident, name in repositories],
            "responses": [
                {field: item[field] for field in ("name", "source", "status")}
                for item in pack["responses"]
            ],
            "facts": pack["expected_facts"],
            "limitations": pack["limitations"],
        })
    assert response.json() == {"providers": expected}
    assert {filename: (PACKS / filename).read_bytes() for filename in packs} == packs


def test_missing_pack_returns_unavailable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "PACK_DIR", tmp_path)
    (tmp_path / "azure_devops.json").write_bytes((PACKS / "azure_devops.json").read_bytes())

    response = client.get(ROUTE, headers=VIEWER)
    assert response.status_code == 503
    assert response.json() == {"detail": "Synthetic provider packs unavailable"}
