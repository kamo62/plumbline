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
        for name, status in [('active', 'running'), ('baseline', 'succeeded'),
                             ('linked', 'succeeded'), ('measured', 'succeeded'),
                             ('expired', 'succeeded')]:
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
    import asyncio
    from plumbline.collection.store import link_run_snapshot, persist_snapshot
    from plumbline.measurements.store import create_measurement
    from plumbline.persistence.models import init_engine_and_tables
    from plumbline.providers.synthetic import SyntheticProvider, WINDOW
    snapshot = asyncio.run(SyntheticProvider().collect(['Alpha'], WINDOW))
    with init_engine_and_tables(url).begin() as conn:
        snapshot_id = persist_snapshot(conn, snapshot)
        link_run_snapshot(conn, paths['linked'].name, snapshot_id)
        link_run_snapshot(conn, paths['measured'].name, snapshot_id)
        create_measurement(conn, paths['measured'].name, minimum_sample=1,
                           population_policy='ado-reported-total/v1')
        from sqlalchemy import text
        conn.execute(text('DELETE FROM run_evidence WHERE run_id=:id'), {'id': paths['measured'].name})
    orphan = root / str(uuid4())
    orphan.mkdir()
    os.utime(orphan, (1, 1))
    assert cleanup_once(root, 30, dry_run=True) == 0
    assert paths['expired'].exists()
    assert cleanup_once(root, 30) == 1
    assert not paths['expired'].exists()
    assert all(paths[name].exists() for name in ('active', 'baseline', 'linked', 'measured')) and orphan.exists()
