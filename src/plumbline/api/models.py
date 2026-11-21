from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from plumbline.config.access import checked_path, validate_org, validate_target


class CollectionConfig(BaseModel):
    """
    API request model for triggering metrics collection.

    Mirrors CLI collect_raw options.
    """

    model_config = ConfigDict(extra="forbid")

    # Selection
    connection_id: str | None = Field(None, min_length=1, description="Saved connection identifier")
    connection_provider: str | None = Field(None, description="Effective provider recorded for queued jobs")
    org: str | None = Field(None, description="Organization identifier (overrides PLUMBLINE_AZURE_DEVOPS_ORG)")
    project: str | None = Field(None, description="Single project to collect from")
    projects: list[str] | None = Field(None, description="Selected projects to collect together")
    all_projects: bool = Field(False, description="Collect from all available projects")

    # Outputs
    write_files: bool = Field(True, description="Write outputs to local files")
    write_db: bool = Field(True, description="Write outputs to database")
    output_path: str | None = Field(None, description="Custom output path (default: exports/{run_id}/metrics)")

    # Assessments
    include_maturity: bool = Field(True, description="Include maturity assessment")

    # Metadata
    run_label: str | None = Field(None, description="Human-friendly label for this run")
    repo: str | None = Field(None, description="Repository name (for metadata)")
    branch: str | None = Field(None, description="Branch name (for metadata)")
    commit_sha: str | None = Field(None, description="Commit SHA (for metadata)")

    # Advanced options
    db_strict: bool = Field(False, description="Fail-fast if DB requested but unavailable")

    @field_validator("connection_provider")
    @classmethod
    def validate_connection_provider(cls, value):
        if value is not None:
            from plumbline.providers.registry import get_provider
            get_provider(value)
        return value

    @model_validator(mode="after")
    def validate_config(self):  # type: ignore[override]
        if self.org:
            self.org = validate_org(self.org)
        if self.project is not None:
            self.project = self.project.strip()
            if not self.project:
                raise ValueError("Project must not be empty")
            validate_target(self.project)
        if self.projects is not None:
            if not self.projects or any(not name.strip() for name in self.projects):
                raise ValueError("Projects must contain nonempty names")
            self.projects = list(dict.fromkeys(name.strip() for name in self.projects))
            for name in self.projects:
                validate_target(name)
        if self.output_path:
            checked_path(self.output_path)

        # Selection constraints
        if sum((self.project is not None, self.projects is not None, self.all_projects)) != 1:
            raise ValueError("Specify exactly one of 'project', 'projects', or 'all_projects'")

        return self


class RunRequest(CollectionConfig):
    """Remote callers use server-owned output storage; CLI keeps custom paths."""

    output_path: None = Field(None, description="API runs use server-owned output storage")
    connection_provider: SkipJsonSchema[str | None] = Field(None, exclude=True)
    db_strict: SkipJsonSchema[bool] = Field(False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def reject_internal_controls(cls, value):
        if isinstance(value, dict) and {"connection_provider", "db_strict"} & value.keys():
            raise ValueError("Internal collection controls cannot be supplied")
        return value
