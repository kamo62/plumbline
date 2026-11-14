import os
from datetime import datetime, timezone
from uuid import uuid4

from scripts.cleanup_exports import cleanup_once
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import RawMetric, Run, session_for


def test_retention_preserves_active_unknown_and_saved_evidence(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'retention.db'}"
    monkeypatch.setenv('PLUMBLINE_DATABASE_URL', url)
    migrate(url)
    root = tmp_path / 'exports'
    root.mkdir()
    paths = {}
    with session_for(url) as db:
        for name, status in [('active', 'running'), ('baseline', 'succeeded'), ('expired', 'succeeded')]:
            run_id = uuid4()
            directory = root / str(run_id)
            directory.mkdir()
            (directory / 'evidence.json').write_text('{}')
            os.utime(directory, (1, 1))
            paths[name] = directory
            db.add(Run(id=run_id, executed_at=datetime.now(timezone.utc), status=status, artifacts_dir=str(directory)))
            db.flush()
            if name == 'baseline':
                db.add(RawMetric(run_id=run_id, source_system='synthetic', source_key='baseline', raw_data={}, collected_at=datetime.now(timezone.utc)))
        db.commit()
    orphan = root / str(uuid4())
    orphan.mkdir()
    os.utime(orphan, (1, 1))
    assert cleanup_once(root, 30, dry_run=True) == 0
    assert paths['expired'].exists()
    assert cleanup_once(root, 30) == 1
    assert not paths['expired'].exists()
    assert paths['active'].exists() and paths['baseline'].exists() and orphan.exists()
