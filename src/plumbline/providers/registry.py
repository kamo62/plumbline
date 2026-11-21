"""Supported saved-connection providers and their diagnostic policies."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from pydantic import SecretStr

from plumbline.config.access import validate_org, validate_target
from .ado import ADOProvider, CAPABILITIES
from .contract import Window
from .doctor import DoctorResult, doctor
from .synthetic import DATA, WINDOW, SyntheticProvider


class UnsupportedProvider(ValueError):
    """The provider key is not supported by saved connections."""


def normalise_projects(projects):
    """Apply the saved-connection project rules and selection normalisation."""
    if not isinstance(projects, list) or len(projects) > 100:
        raise ValueError("Invalid project")
    result = []
    for project in projects:
        if (not isinstance(project, str) or not project.strip() or len(project) > 128
                or any(ord(char) < 32 for char in project)):
            raise ValueError("Invalid project")
        project = project.strip()
        validate_target(project)
        if project not in result:
            result.append(project)
    return result


async def _ado_diagnostic(connection_id, organisation, projects, credential):
    from plumbline.backend.api.ado_client import AzureDevOpsClient
    from plumbline.connections.execution import protect_logs
    from plumbline.config.settings import Settings

    settings = Settings(azure_devops_org=organisation, azure_devops_pat=SecretStr(credential))
    end = datetime.now(timezone.utc)
    with protect_logs(credential):
        async with AzureDevOpsClient(settings) as client:
            return await doctor(ADOProvider(client, connection_id), projects,
                                Window(end - timedelta(days=30), end, end))


async def _synthetic_diagnostic(connection_id, organisation, projects, credential):
    return await doctor(SyntheticProvider(connection_id), projects, WINDOW)


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    status: str
    organisation_required: bool
    projects_required: bool
    credential_required_for_execution: bool
    default_projects: tuple[str, ...]
    diagnostic: Callable[..., Awaitable[DoctorResult]]

    def connection_inputs(self, organisation, config, credential_present):
        if not isinstance(config, dict) or set(config) - {"projects"}:
            raise ValueError("Invalid connection input")
        projects = normalise_projects(config.get("projects", []))
        if self.organisation_required:
            try:
                organisation = validate_org(organisation or "")
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Invalid organisation") from None
        elif organisation is not None or credential_present:
            raise ValueError("Synthetic connections do not accept organisation or credentials")
        if self.projects_required and not projects:
            raise ValueError("ADO requires at least one project")
        return organisation, projects or list(self.default_projects)

    def metadata(self):
        # A capability indicates an implemented evidence family, not completeness
        # or successful verification against a live provider.
        return {
            "id": self.id, "label": self.label, "status": self.status,
            "capabilities": sorted(CAPABILITIES),
            "requirements": {
                "organisation": "required" if self.organisation_required else "not_supported",
                "projects": "required" if self.projects_required else "optional",
                "credential": "required_for_execution" if self.credential_required_for_execution else "not_supported",
            },
        }


_PROVIDERS = {
    spec.id: spec for spec in (
        ProviderSpec("ado", "Azure DevOps", "implemented_live_unverified", True, True, True, (), _ado_diagnostic),
        ProviderSpec("synthetic", "Synthetic dataset", "diagnostic_only", False, False, False,
                     tuple(DATA), _synthetic_diagnostic),
    )
}


def get_provider(key):
    try:
        return _PROVIDERS[key]
    except (KeyError, TypeError):
        raise UnsupportedProvider("Connection provider is unsupported") from None


def provider_metadata():
    return [spec.metadata() for spec in _PROVIDERS.values()]
