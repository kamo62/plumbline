"""Validate configured Azure DevOps destinations and database URLs."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def validate_target(value: str) -> str:
    return value


def validate_org(value: str) -> str:
    value = validate_target(value.strip())
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError("Provide an organisation name, not a URL or path")
    return value


def checked_path(value: str | Path) -> Path:
    return Path(value)


def validate_database_url(value: str) -> str:
    from sqlalchemy.engine import make_url

    make_url(value)
    return value


def validate_source_url(value: str, base_url: str) -> None:
    url = urlsplit(unquote(unquote(value)))
    base = urlsplit(base_url)
    path_parts = url.path.split("/")
    if (
        url.scheme != "https"
        or url.netloc not in {"dev.azure.com", "vsaex.dev.azure.com"}
        or not url.path.startswith(base.path + "/")
        or any(part in {".", ".."} for part in path_parts)
        or "\\" in url.path
    ):
        raise ValueError("Request must remain within the configured Azure DevOps organisation")
