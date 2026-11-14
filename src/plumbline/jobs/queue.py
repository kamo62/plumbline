"""Transactional queue operations shared by the API and worker."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlmodel import Session

from plumbline import __version__
from plumbline.jobs.models import Job
from plumbline.persistence.models import Run

MAX_ATTEMPTS = 3

def now():
    return datetime.now(timezone.utc)


def enqueue(engine, config, settings):
    timestamp = now()
    run_id = uuid4()
    with Session(engine) as session, session.begin():
        if config.connection_id:
            from plumbline.connections.execution import effective_config
            config, _ = effective_config(session, config)
        session.add(Run(id=run_id, executed_at=timestamp, status="queued", org=config.org or settings.azure_devops_org,
                        project=config.project, repo=config.repo, branch=config.branch, commit_sha=config.commit_sha,
                        run_label=config.run_label, environment=settings.run_env, tool_version=__version__))
        session.flush()
        session.add(Job(id=uuid4(), run_id=run_id, config=config.model_dump(mode="json"), status="queued", attempts=0, created_at=timestamp))
    return str(run_id)


def claim(engine, lease_seconds=60):
    timestamp = now()
    owner = str(uuid4())
    expired = and_(Job.status == "running", Job.lease_until < timestamp)
    eligible = or_(Job.status == "queued", expired)
    candidate = select(Job.id).where(eligible, Job.cancel_requested_at.is_(None)).order_by(Job.created_at, Job.id).limit(1).scalar_subquery()
    with Session(engine) as session, session.begin():
        exhausted = session.execute(update(Job).where(eligible, Job.attempts >= MAX_ATTEMPTS,
            Job.cancel_requested_at.is_(None)).values(status="failed", lease_until=None,
            error="Recovery attempt limit reached after lease expiry or shutdown").returning(Job.run_id)).scalars().all()
        if exhausted:
            session.execute(update(Run).where(Run.id.in_(exhausted)).values(status="failed", finished_at=timestamp,
                error="Recovery attempt limit reached after lease expiry or shutdown"))
        row = session.execute(update(Job).where(Job.id == candidate, eligible, Job.attempts < MAX_ATTEMPTS, Job.cancel_requested_at.is_(None)).values(
            status="running", owner=owner, lease_until=timestamp + timedelta(seconds=lease_seconds), heartbeat_at=timestamp,
            attempts=Job.attempts + 1).returning(Job.run_id, Job.config, Job.attempts)).first()
        if row:
            session.execute(update(Job).where(Job.run_id == row.run_id).values(result={
                "attempt": row.attempts, "max_attempts": MAX_ATTEMPTS,
                "recovery_reason": "previous attempt expired or stopped" if row.attempts > 1 else None,
                "repositories": {}}))
            session.execute(update(Run).where(Run.id == row.run_id).values(status="running", started_at=timestamp, finished_at=None, error=None))
            return str(row.run_id), owner, row.config
    return None


def heartbeat(engine, run_id, owner, lease_seconds=60):
    timestamp = now()
    with Session(engine) as session, session.begin():
        row = session.execute(update(Job).where(Job.run_id == run_id, Job.owner == owner, Job.status == "running",
                                                 Job.lease_until > timestamp).values(
            heartbeat_at=timestamp, lease_until=timestamp + timedelta(seconds=lease_seconds)).returning(Job.cancel_requested_at)).first()
        return (row is not None, bool(row and row[0]))


def cancel(engine, run_id):
    timestamp = now()
    with Session(engine) as session, session.begin():
        row = session.execute(update(Job).where(Job.run_id == run_id, Job.status.in_(("queued", "running"))).values(
            cancel_requested_at=timestamp,
            status=case((Job.status == "queued", "cancelled"), else_=Job.status)).returning(Job.status)).first()
        if row is None:
            return session.execute(select(Job.status).where(Job.run_id == run_id)).scalar_one_or_none()
        if row[0] == "cancelled":
            session.execute(update(Run).where(Run.id == run_id).values(status="cancelled", finished_at=timestamp))
            return "cancelled"
        return "cancelling"


def finish(engine, run_id, owner, status, result=None, error=None, persist=None):
    """Fence all snapshot and terminal writes under the same ownership lock."""
    timestamp = now()
    with engine.begin() as conn:
        # The conditional write locks the row on both databases before any snapshot changes.
        locked = conn.execute(update(Job).where(Job.run_id == run_id, Job.owner == owner, Job.status == "running",
                                                Job.lease_until > timestamp).values(heartbeat_at=timestamp)
                              .returning(Job.cancel_requested_at, Job.result)).first()
        if not locked:
            return False
        if locked[0] is not None:
            status, result, error, persist = "cancelled", None, None, None
        if persist:
            persist(conn)
        merged = {**(locked[1] or {}), **(result or {})}
        conn.execute(update(Job).where(Job.run_id == run_id).values(status=status, result=merged, error=error, lease_until=None))
        conn.execute(update(Run).where(Run.id == run_id).values(status=status, finished_at=now(), error=error,
                     artifacts_dir=(result or {}).get("artifacts_dir")))
    return True


def progress(engine, run_id, owner, item):
    """Update one repository while holding the same ownership fence as completion."""
    timestamp = now()
    with engine.begin() as conn:
        row = conn.execute(update(Job).where(Job.run_id == run_id, Job.owner == owner,
            Job.status == "running", Job.lease_until > timestamp,
            Job.cancel_requested_at.is_(None)).values(heartbeat_at=Job.heartbeat_at)
            .returning(Job.result)).first()
        if row is None:
            return False
        result = dict(row[0] or {})
        repositories = dict(result.get("repositories") or {})
        key = f"{item['project_id']}:{item['repository_id']}"
        repositories[key] = item
        result["repositories"] = repositories
        conn.execute(update(Job).where(Job.run_id == run_id).values(result=result))
    return True


def release(engine, run_id, owner):
    timestamp = now()
    with engine.begin() as conn:
        row = conn.execute(update(Job).where(Job.run_id == run_id, Job.owner == owner,
            Job.status == "running", Job.lease_until > timestamp).values(
                status=case((Job.cancel_requested_at.is_not(None), "cancelled"), else_="queued"),
                lease_until=None, owner=None).returning(Job.status)).first()
        if row:
            conn.execute(update(Run).where(Run.id == run_id).values(status=row[0],
                finished_at=timestamp if row[0] == "cancelled" else None))
        return bool(row)


def recover_cancelled(engine):
    timestamp = now()
    with Session(engine) as session, session.begin():
        ids = session.execute(update(Job).where(Job.status == "running", Job.lease_until < timestamp,
                                                Job.cancel_requested_at.is_not(None)).values(status="cancelled", lease_until=None)
                              .returning(Job.run_id)).scalars().all()
        if ids:
            session.execute(update(Run).where(Run.id.in_(ids)).values(status="cancelled", finished_at=timestamp))
