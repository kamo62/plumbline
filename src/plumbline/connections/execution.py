"""Resolve saved connections at the queue and execution boundaries."""
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from contextlib import contextmanager
import logging
import base64

from pydantic import SecretStr
from sqlmodel import Session

from plumbline.connections.models import Connection
from plumbline.connections.service import CredentialError, decrypt
from plumbline.providers.registry import UnsupportedProvider, get_provider


class ConnectionValidationError(ValueError):
    """A selected connection or collection scope is invalid."""


def effective_config(session, config):
    item = session.get(Connection, config.connection_id)
    if item is None:
        raise ConnectionValidationError("Connection not found")
    try:
        spec = get_provider(item.provider)
    except UnsupportedProvider:
        raise ConnectionValidationError("Connection provider is unsupported") from None
    if config.connection_provider and config.connection_provider != item.provider:
        raise ConnectionValidationError("Connection provider changed")
    try:
        org, projects = spec.connection_inputs(item.organisation, item.config, item.credential_ciphertext is not None)
        org = org if spec.organisation_required else spec.id
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
        if get_provider(provider).credential_required_for_execution:
            if not item.credential_ciphertext:
                raise ConnectionValidationError("Connection credential is missing")
            try:
                credential = decrypt(item.credential_ciphertext)
            except CredentialError:
                raise ConnectionValidationError("Connection credential is unavailable") from None
            settings = settings.model_copy(update={"azure_devops_org": effective.org,
                                                   "azure_devops_pat": SecretStr(credential)})
        return effective, settings, provider


def redact(value, secret):
    if not secret:
        return value
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        normalized = secret.strip().strip('"').strip("'").strip()
        if not normalized:
            return value
        forms = {secret, normalized}
        if normalized:
            forms.add(base64.b64encode(f":{normalized}".encode()).decode())
        for form in sorted((x for x in forms if x), key=len, reverse=True):
            value = value.replace(form, "[REDACTED]")
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return replace(value, **{field.name: redact(getattr(value, field.name), secret) for field in fields(value)})
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, secret) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(redact(v, secret) for v in value)
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
