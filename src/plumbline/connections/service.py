"""Credential encryption, provider diagnostics and atomic key rotation."""
import os
from dataclasses import asdict

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import text
from sqlmodel import select

from plumbline.auth.service import event
from plumbline.providers import ADOProvider, SyntheticProvider, doctor
from plumbline.providers.synthetic import WINDOW
from .models import Connection


class CredentialError(Exception):
    """A key or encrypted credential cannot be used."""


def cipher(key=None):
    try:
        return Fernet((key if key is not None else os.environ.get("PLUMBLINE_ENCRYPTION_KEY", "")).encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        raise CredentialError("Encryption key is missing or invalid") from None


def encrypt(value, key=None):
    return cipher(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value, key=None):
    try:
        return cipher(key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError, AttributeError):
        raise CredentialError("Credential cannot be decrypted") from None


def lock(db):
    if db.bind.dialect.name == "sqlite":
        db.exec(text("BEGIN IMMEDIATE"))
    elif db.bind.dialect.name == "postgresql":
        db.exec(text("SELECT pg_advisory_xact_lock(786512106)"))
    else:
        raise ValueError("Unsupported database")


def rotate(db, old_key, new_key):
    """Re-encrypt all credentials in one locked transaction, or roll back all."""
    try:
        lock(db)
        old, new = cipher(old_key), cipher(new_key)
        rows = db.exec(select(Connection)).all()
        count = 0
        for row in rows:
            if row.credential_ciphertext is not None:
                plaintext = old.decrypt(row.credential_ciphertext.encode("ascii"))
                row.credential_ciphertext = new.encrypt(plaintext).decode("ascii")
                del plaintext
                db.add(row)
                count += 1
        event(db, "connection_keys_rotated")
        db.commit()
        return count
    except Exception as exc:
        db.rollback()
        if isinstance(exc, (InvalidToken, ValueError, UnicodeError, CredentialError)):
            raise CredentialError("Key rotation failed; no connections changed") from None
        raise


async def run_doctor(item):
    if item.provider == "synthetic":
        result = await doctor(SyntheticProvider(item.id), item.config.get("projects") or ["Alpha", "Beta"], WINDOW)
    else:
        if not item.credential_ciphertext:
            raise CredentialError("Credential is missing")
        from plumbline.backend.api.ado_client import AzureDevOpsClient
        from plumbline.config.settings import Settings
        from plumbline.providers.contract import Window
        from datetime import datetime, timedelta, timezone

        credential = decrypt(item.credential_ciphertext)
        try:
            settings = Settings(azure_devops_org=item.organisation, azure_devops_pat=SecretStr(credential))
            end = datetime.now(timezone.utc)
            async with AzureDevOpsClient(settings) as client:
                result = await doctor(ADOProvider(client, item.id), item.config["projects"],
                                      Window(end - timedelta(days=30), end, end))
        finally:
            del credential
    return asdict(result)
