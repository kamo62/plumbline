"""Credential primitives and transaction helpers for local authentication."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import select
from .models import ApiToken, Attempt, AuditEvent, SessionRecord, User


def now():
    return datetime.utcnow()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${value.hex()}"


def verify_password(password, encoded):
    try:
        name, n, r, p, salt, expected = encoded.split("$")
        if name != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def event(db, action, actor=None, subject=None):
    db.add(AuditEvent(id=str(uuid4()), occurred_at=now(), actor_id=actor, action=action, subject_id=subject))


def lock_accounts(db):
    """Serialise bootstrap and administrator changes across application processes."""
    if db.bind.dialect.name == "sqlite":
        db.exec(text("BEGIN IMMEDIATE"))
    elif db.bind.dialect.name == "postgresql":
        db.exec(text("SELECT pg_advisory_xact_lock(786512105)"))
    else:
        raise ValueError("Unsupported database")


def bootstrap(db, username, password):
    lock_accounts(db)
    if db.exec(select(User)).first():
        raise ValueError("Bootstrap is only available before the first account exists")
    if len(password) < 12 or not username.strip():
        raise ValueError("Username and a password of at least 12 characters are required")
    user = User(id=str(uuid4()), username=username.strip().lower(), password_hash=password_hash(password),
                role="admin", active=True, force_change=True)
    db.add(user)
    event(db, "bootstrap", user.id, user.id)
    db.commit()
    return user


def revoke_all(db, user_id):
    for model in (SessionRecord, ApiToken):
        for item in db.exec(select(model).where(model.user_id == user_id)).all():
            db.delete(item)


def authenticate(db, username, password):
    lock_accounts(db)
    username = username.strip().lower()
    key = digest(username)
    attempt = db.get(Attempt, key)
    if attempt and attempt.failures >= 5 and attempt.until > now():
        return None, True
    user = db.exec(select(User).where(User.username == username)).first()
    valid = bool(user and user.active and verify_password(password, user.password_hash))
    if not valid:
        if not attempt:
            attempt = Attempt(key=key, failures=0, until=now())
        if attempt.until <= now():
            attempt.failures = 0
        attempt.failures += 1
        attempt.until = now() + timedelta(minutes=15)
        db.add(attempt)
        event(db, "login_failed", subject=user.id if user else None)
        db.commit()
        return None, False
    if attempt:
        db.delete(attempt)
    event(db, "login", user.id, user.id)
    db.commit()
    return user, False
