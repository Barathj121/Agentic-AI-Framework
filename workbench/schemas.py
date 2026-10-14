from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceEstimate(Contract):
    cpu: str = "low"
    memory: str = "low"
    execution_time: str = "short"


def relative_location(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not value or PureWindowsPath(value).drive or normalized.startswith("/") or ".." in PurePosixPath(normalized).parts or ":" in normalized or "\x00" in value:
        raise ValueError("Location must be a safe workspace-relative path")
    return normalized


class OutputSpec(Contract):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=500)
    required_columns: list[str] = Field(default_factory=list)
    min_rows: int | None = Field(default=None, ge=0)
    allow_empty: bool = False
    description: str = ""

    @model_validator(mode="after")
    def valid_location(self):
        if self.type in {"database_table", "database_view"}:
            import re
            if not re.fullmatch(r"(?:staging|features|results)\.[A-Za-z_][A-Za-z0-9_]*", self.location):
                raise ValueError("Database outputs must be schema.table in staging, features or results")
        else:
            self.location = relative_location(self.location)
            if self.location.split("/")[0] not in {"results", "models", "notebooks", "generated"}:
                raise ValueError("Output files must be in results, models, notebooks or generated")
        return self


class Proposal(Contract):
    proposal_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    title: str = Field(min_length=1, max_length=200)
    description: str
    approach: str
    implementation_steps: list[str]
    required_columns: list[str]
    expected_outputs: list[OutputSpec] = Field(min_length=1)
    advantages: list[str]
    limitations: list[str]
    assumptions: list[str]
    resource_estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    validation_strategy: list[str]
    feasibility: Literal["FEASIBLE", "FEASIBLE_WITH_LIMITATIONS", "NOT_FEASIBLE"]
    confidence_explanation: str
    required_dependencies: list[str] = Field(default_factory=list)


class Understanding(Contract):
    summary: str
    relevant_columns: list[str]
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    exploration_queries: list[str] = Field(default_factory=list, max_length=3)


class ProposalSet(Contract):
    proposals: list[Proposal] = Field(min_length=1, max_length=3)

    @field_validator("proposals")
    @classmethod
    def unique_ids(cls, proposals):
        if len({p.proposal_id for p in proposals}) != len(proposals):
            raise ValueError("Proposal IDs must be unique")
        return proposals


class GeneratedFile(Contract):
    path: str
    content: str = Field(max_length=200_000)

    @field_validator("path")
    @classmethod
    def generated_only(cls, path):
        path = relative_location(path)
        if not path.startswith("generated/") or PurePosixPath(path).suffix.lower() not in {".py", ".sql", ".json", ".txt", ".md"}:
            raise ValueError("Generated files must be Python/SQL/JSON/text under generated/")
        return path


class ValidationCheck(Contract):
    name: str
    kind: Literal["file_exists", "table_exists", "row_count", "columns", "sql_assertion", "model_load", "json_valid", "notebook_valid"]
    target: str
    sql: str | None = None
    min_rows: int | None = Field(default=None, ge=0)
    required_columns: list[str] = Field(default_factory=list)


class BuildResult(Contract):
    files: list[GeneratedFile] = Field(min_length=1, max_length=20)
    entrypoint: Literal["generated/solution.py"] = "generated/solution.py"
    required_dependencies: list[str] = Field(default_factory=list)
    expected_outputs: list[OutputSpec] = Field(min_length=1)
    validation_checks: list[ValidationCheck] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def entry_exists(self):
        paths = [f.path for f in self.files]
        if self.entrypoint not in paths or len(paths) != len(set(paths)):
            raise ValueError("Build must have one generated/solution.py and no duplicate paths")
        if sum(len(f.content) for f in self.files) > 500_000:
            raise ValueError("Generated source exceeds 500 KB")
        return self


class ExecutionResult(Contract):
    success: bool
    exit_code: int | None
    execution_time_seconds: float
    stdout_path: str
    stderr_path: str
    timed_out: bool = False
    cancelled: bool = False
    oom_killed: bool = False
    error: str | None = None
