#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from plumbline.config.access import checked_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clean up old export directories by age")
    p.add_argument("--path", default=str(Path("exports").resolve()), help="Exports root path (default: ./exports)")
    p.add_argument("--retention-days", type=int, default=int(os.getenv("PLUMBLINE_EXPORTS_RETENTION_DAYS", "30")), help="Retention window in days (default: 30)")
    p.add_argument("--dry-run", action="store_true", help="List what would be deleted without removing")
    p.add_argument("--interval-hours", type=int, default=0, help="If >0, run continuously with this interval in hours")
    return p.parse_args()


def cleanup_once(root: Path, retention_days: int, dry_run: bool = False) -> int:
    root = checked_path(root)
    now = datetime.now()
    cutoff = now - timedelta(days=retention_days)
    if not root.exists():
        print(f"[cleanup] Path does not exist: {root}")
        return 0

    from sqlalchemy import text
    from plumbline.config.settings import Settings
    from plumbline.persistence.models import init_engine_and_tables
    from uuid import UUID
    import shutil

    url = Settings().database_url
    if not url:
        raise RuntimeError("Retention requires a configured, migrated database")
    engine = init_engine_and_tables(url)
    removed = 0
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            run_id = str(UUID(entry.name))
        except ValueError:
            continue  # Unknown ownership is protected.
        if datetime.fromtimestamp(entry.stat().st_mtime) > cutoff:
            continue
        with engine.begin() as connection:
            # Lock the owning row before inspecting state or removing its files.
            connection.execute(text("UPDATE runs SET artifacts_dir=artifacts_dir WHERE id=:id"), {"id": run_id})
            row = connection.execute(text("SELECT status, artifacts_dir FROM runs WHERE id=:id"), {"id": run_id}).first()
            if not row or row[0] not in {"succeeded", "failed", "cancelled"} or not row[1]:
                continue
            if Path(row[1]).resolve() != entry.resolve():
                continue
            active = connection.execute(text("SELECT COUNT(*) FROM jobs WHERE run_id=:id AND status IN ('queued','running')"), {"id": run_id}).scalar()
            snapshots = connection.execute(text("SELECT (SELECT COUNT(*) FROM raw_metrics WHERE run_id=:id) + (SELECT COUNT(*) FROM metrics WHERE run_id=:id) + (SELECT COUNT(*) FROM run_evidence WHERE run_id=:id) + (SELECT COUNT(*) FROM metric_measurements WHERE run_id=:id)"), {"id": run_id}).scalar()
            if active or snapshots:
                continue  # Preserve all stored baselines until expiry is modelled explicitly.
            targets = [entry, *entry.rglob("*")]
            for target in targets:
                print(f"[cleanup] {'DRY RUN target' if dry_run else 'Removal target'}: {target}")
            if not dry_run:
                shutil.rmtree(entry)
                connection.execute(text("UPDATE runs SET artifacts_dir=NULL WHERE id=:id"), {"id": run_id})
                removed += 1

    print(f"[cleanup] Done. Removed {removed} directorie(s). Retention={retention_days}d Path={root}")
    return removed


def main() -> int:
    args = parse_args()
    root = Path(args.path).resolve()
    if args.interval_hours and args.interval_hours > 0:
        interval_sec = max(60, args.interval_hours * 3600)
        print(f"[cleanup] Starting loop. Path={root} Retention={args.retention_days}d Interval={args.interval_hours}h DryRun={args.dry_run}")
        try:
            while True:
                cleanup_once(root, args.retention_days, args.dry_run)
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            print("[cleanup] Exiting on Ctrl+C")
            return 0
    else:
        cleanup_once(root, args.retention_days, args.dry_run)
        return 0


if __name__ == "__main__":
    sys.exit(main())
