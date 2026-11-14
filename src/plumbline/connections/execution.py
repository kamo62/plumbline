"""Resolve saved connections at the queue and execution boundaries."""
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from contextlib import contextmanager
import logging

from pydantic import SecretStr
from sqlmodel import Session

from plumbline.config.access import validate_org, validate_target
from plumbline.connections.models import Connection
from plumbline.connections.service import CredentialError, decrypt
from plumbline.providers import SyntheticProvider
from plumbline.providers.synthetic import WINDOW


class ConnectionValidationError(ValueError):
    """A selected connection or collection scope is invalid."""


def effective_config(session, config):
    item = session.get(Connection, config.connection_id)
    if item is None:
        raise ConnectionValidationError("Connection not found")
    if item.provider not in {"ado", "synthetic"}:
        raise ConnectionValidationError("Connection provider is unsupported")
    if config.connection_provider and config.connection_provider != item.provider:
        raise ConnectionValidationError("Connection provider changed")
    try:
        org = validate_org(item.organisation or "") if item.provider == "ado" else "synthetic"
        projects = item.config.get("projects") or ([] if item.provider == "ado" else ["Alpha", "Beta"])
        if not isinstance(projects, list) or not projects or any(not isinstance(p, str) or not p.strip() for p in projects):
            raise ValueError
        for project in projects:
            validate_target(project)
    except (ValueError, TypeError, AttributeError):
        raise ConnectionValidationError("Connection configuration is invalid") from None
    if config.org and config.org != org:
        raise ConnectionValidationError("Connection organisation does not match requested organisation")
    selected = projects if config.all_projects else (config.projects if config.projects is not None else [config.project])
    if not set(selected).issubset(set(projects)):
        raise ConnectionValidationError("Requested projects are outside connection scope")
    return config.model_copy(update={"org": org, "project": None, "projects": list(selected), "all_projects": False,
                                     "connection_provider": item.provider}), item


def resolve(engine, config, settings):
    """Revalidate the persisted scope and decrypt only immediately before use."""
    with Session(engine) as session:
        effective, item = effective_config(session, config)
        provider = item.provider
        if provider == "ado":
            if not item.credential_ciphertext:
                raise ConnectionValidationError("Connection credential is missing")
            try:
                credential = decrypt(item.credential_ciphertext)
            except CredentialError:
                raise ConnectionValidationError("Connection credential is unavailable") from None
            settings = settings.model_copy(update={"azure_devops_org": effective.org,
                                                   "azure_devops_pat": SecretStr(credential)})
        return effective, settings, provider


def _json(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json(v) for v in value]
    return value


async def synthetic_data(config):
    """Carry all provider evidence through the existing raw-data sinks without scoring it."""
    snapshot = await SyntheticProvider(config.connection_id).collect(config.projects, WINDOW)
    evidence = _json(asdict(snapshot))
    failures = [{"scope": kind, "project_id": record["identity"]["project"],
                 "field": name, "state": field["state"], "error": field["error_code"]}
                for kind in ("projects", "repositories", "builds")
                for record in evidence[kind] for name, field in record["fields"].items()
                if field["state"] not in ("observed", "observed_empty")]
    # This provider's diagnostic evidence is not a complete legacy assessment input.
    failures.append({"scope": "collection", "error": "Synthetic evidence is not a complete assessment input"})
    return {"provider": "synthetic", "evidence": evidence,
            "completeness": {"complete": False, "failures": failures}}, snapshot


def redact(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, secret) for v in value)
    return value


@contextmanager
def protect_logs(secret):
    class Filter(logging.Filter):
        def filter(self, record):
            record.msg = redact(record.getMessage(), secret)
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            for key, value in vars(record).copy().items():
                if key not in ("msg", "args"):
                    setattr(record, key, redact(value, secret))
            return True
    guard = Filter()
    loggers = [logging.getLogger(name) for name in (
        "plumbline.runner", "plumbline.backend.api.ado_client",
        "plumbline.backend.services.raw_data_collector")]
    for logger in loggers:
        logger.addFilter(guard)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(guard)
