"""Read-only projection of the fictional SCM packs in the source distribution."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, JsonValue


PACK_DIR = Path(__file__).resolve().parents[3] / "synthetic_data"
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/demo", tags=["demo"])


class DemoWindow(BaseModel):
    start: str
    end_exclusive: str
    observed_at: str


class DemoRepository(BaseModel):
    id: str
    name: str


class DemoResponse(BaseModel):
    name: str
    source: str
    status: int


class DemoProvider(BaseModel):
    id: str
    label: str
    api_version: str
    window: DemoWindow
    repositories: list[DemoRepository]
    responses: list[DemoResponse]
    facts: dict[str, JsonValue]
    limitations: list[str]


class DemoProviders(BaseModel):
    providers: list[DemoProvider]


def _read_provider(
    filename: str,
    label: str,
    *,
    inventory_source: str = "repositories",
    items_key: str | None = None,
    id_key: str = "id",
) -> DemoProvider:
    pack = json.loads((PACK_DIR / filename).read_bytes())
    repositories = []
    responses = []
    for response in pack["responses"]:
        responses.append(DemoResponse.model_validate(response, strict=True))
        if response["source"] != inventory_source:
            continue
        body = response["body"]
        items = body if items_key is None else body[items_key]
        if not isinstance(items, list):
            raise ValueError("Expected a repository inventory list")
        for repository in items:
            identifier = repository[id_key]
            if type(identifier) not in (str, int):
                raise ValueError("Expected a string or numeric repository ID")
            repositories.append(DemoRepository(id=str(identifier), name=repository["name"]))

    return DemoProvider.model_validate({
        "id": pack["provider"],
        "label": label,
        "api_version": pack["api_version"],
        "window": pack["window"],
        "repositories": repositories,
        "responses": responses,
        "facts": pack["expected_facts"],
        "limitations": pack["limitations"],
    }, strict=True)


@router.get("/providers")
def get_demo_providers() -> DemoProviders:
    """Show the baseline evidence published in the four fictional SCM packs."""
    try:
        return DemoProviders(providers=[
            _read_provider("azure_devops.json", "Azure DevOps", items_key="value"),
            _read_provider("github.json", "GitHub"),
            _read_provider("gitlab.json", "GitLab", inventory_source="projects"),
            _read_provider("bitbucket.json", "Bitbucket Cloud", items_key="values", id_key="uuid"),
        ])
    except (OSError, ValueError, TypeError, KeyError):
        logger.exception("Could not read the synthetic provider packs")
        raise HTTPException(503, "Synthetic provider packs unavailable") from None
