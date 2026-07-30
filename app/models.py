from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal[3] = 3
Sha = Annotated[str, Field(min_length=1, max_length=128)]
EnvironmentName = Annotated[str, Field(min_length=1, max_length=255)]
CANONICAL_ENVIRONMENTS = frozenset({"dev", "test", "stage", "prod"})


def environment_partition_key(project_id: int, environment: str) -> str:
    return f"{project_id}:{environment}"


def deployment_key(project_id: int, deployment_id: int) -> str:
    return f"{project_id}:{deployment_id}"


def deployment_project_id(event: DeploymentEvent) -> int:
    return event.project_id


class EventStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"


class CompletionReason(StrEnum):
    INITIAL_BASELINE = "initial_baseline"
    NO_CHANGES = "no_changes"


AnalysisOutcome = Literal["complete", "partial"]


def _safe_report_path(value: str) -> str:
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("report path must be a safe relative POSIX path")
    return value


class CompletionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    report_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    report_chars: Annotated[int, Field(ge=1, le=100000)]
    report_bytes: Annotated[int, Field(ge=1, le=512000)]
    report_path: str
    analysis_outcome: AnalysisOutcome
    started_at: datetime

    @field_validator("report_path")
    @classmethod
    def report_path_is_safe(cls, value: str) -> str:
        return _safe_report_path(value)


class CompletionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    report_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    report_chars: Annotated[int, Field(ge=1, le=100000)]
    report_bytes: Annotated[int, Field(ge=1, le=512000)]
    report_path: str
    analysis_outcome: AnalysisOutcome
    completed_at: datetime

    @field_validator("report_path")
    @classmethod
    def report_path_is_safe(cls, value: str) -> str:
        return _safe_report_path(value)


class ProjectSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gitlab_host: str
    project_id: Annotated[int, Field(gt=0)]
    name: str
    web_url: str
    http_repo_url: str
    ssh_repo_url: str


class CommitSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: Sha
    title: Annotated[str, Field(max_length=500)]
    author_name: Annotated[str, Field(max_length=255)]
    authored_date: str
    web_url: str


class ChangedFilePreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    old_path: Annotated[str, Field(max_length=1000)]
    new_path: Annotated[str, Field(max_length=1000)]
    added: bool
    renamed: bool
    deleted: bool
    generated: bool
    collapsed: bool
    too_large: bool


class ComparePreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_files: tuple[ChangedFilePreview, ...] = Field(max_length=200)
    changed_file_count: Annotated[int, Field(ge=0)]
    added_files: Annotated[int, Field(ge=0)]
    renamed_files: Annotated[int, Field(ge=0)]
    deleted_files: Annotated[int, Field(ge=0)]
    generated_files: Annotated[int, Field(ge=0)]
    files_truncated: bool
    compare_timeout: bool
    incomplete: bool
    same_ref: bool
    failure: Annotated[str, Field(max_length=128)] | None = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    claimed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def expiry_follows_claim(self) -> Claim:
        if self.expires_at <= self.claimed_at:
            raise ValueError("claim expires_at must follow claimed_at")
        return self


class DeploymentEvent(BaseModel):
    """Immutable record of one GitLab deployment.

    State transitions create a replacement record; deployment identity can never
    be mutated in place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: Annotated[int, Field(gt=0)]
    deployment_id: Annotated[int, Field(gt=0)]
    environment: EnvironmentName
    sha: Sha
    previous_sha: Sha | None
    finished_at: datetime
    discovered_at: datetime
    status: EventStatus = EventStatus.PENDING
    claim: Claim | None = None
    completed_at: datetime | None = None
    completion_reason: CompletionReason | None = None
    report_path: str | None = None
    completion_intent: CompletionIntent | None = None
    completion_receipt: CompletionReceipt | None = None
    pipeline_id: Annotated[int, Field(gt=0)] | None = None
    # Schema v2 did not retain this factual field. It is required for newly
    # ingested events but remains null after a lossless v2 -> v3 migration.
    pipeline_ref: Annotated[str, Field(min_length=1, max_length=255)] | None
    project: ProjectSnapshot | None = None
    commit: CommitSnapshot | None = None
    compare_preview: ComparePreview | None = None

    @field_validator("report_path")
    @classmethod
    def completed_report_path_is_safe(cls, value: str | None) -> str | None:
        return _safe_report_path(value) if value is not None else None

    @property
    def identity(
        self,
    ) -> tuple[int, int, str, str, str | None, datetime, int | None, str | None]:
        return (
            self.project_id,
            self.deployment_id,
            self.environment,
            self.sha,
            self.previous_sha,
            self.finished_at,
            self.pipeline_id,
            self.pipeline_ref,
        )

    @model_validator(mode="after")
    def validate_state(self) -> DeploymentEvent:
        if self.project is not None and self.project.project_id != self.project_id:
            raise ValueError("project snapshot ID must match authoritative project_id")
        if self.status is EventStatus.PENDING and self.claim is not None:
            raise ValueError("pending event cannot contain a claim")
        if self.status is EventStatus.CLAIMED and self.claim is None:
            raise ValueError("claimed event requires claim metadata")
        if self.status is not EventStatus.CLAIMED and self.claim is not None:
            raise ValueError("claim metadata is only valid for claimed events")
        if self.completion_intent is not None:
            if self.status is not EventStatus.CLAIMED or self.claim is None:
                raise ValueError("completion intent requires a claimed event")
            if self.completion_intent.token_hash != self.claim.token_hash:
                raise ValueError("completion intent token must match the claim")
        if self.status is not EventStatus.CLAIMED and self.completion_intent is not None:
            raise ValueError("completion intent is only valid for claimed events")
        if self.status is not EventStatus.COMPLETED and self.completion_receipt is not None:
            raise ValueError("completion receipt requires completed status")
        if self.completion_receipt is not None:
            if self.completion_reason is not None:
                raise ValueError("report completion cannot have an automatic reason")
            if self.completed_at != self.completion_receipt.completed_at:
                raise ValueError("receipt timestamp must match completed_at")
            if self.report_path != self.completion_receipt.report_path:
                raise ValueError("receipt path must match report_path")
        if self.status is not EventStatus.COMPLETED and (
            self.completed_at is not None
            or self.completion_reason is not None
            or self.report_path is not None
        ):
            raise ValueError("completion fields require completed status")
        if self.completion_reason is CompletionReason.INITIAL_BASELINE and (
            self.status is not EventStatus.COMPLETED or self.previous_sha is not None
        ):
            raise ValueError("initial_baseline must be completed with no previous_sha")
        if self.completion_reason is CompletionReason.NO_CHANGES and (
            self.status is not EventStatus.COMPLETED or self.previous_sha != self.sha
        ):
            raise ValueError("no_changes must be completed with equal deployment SHAs")
        if (
            self.status is EventStatus.COMPLETED
            and self.previous_sha is None
            and self.completion_reason is not CompletionReason.INITIAL_BASELINE
        ):
            raise ValueError("a completed baseline requires initial_baseline")
        return self


class EnvironmentState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    last_seen_deployment_id: Annotated[int, Field(gt=0)] | None = None
    last_seen_sha: Sha | None = None
    last_completed_deployment_id: Annotated[int, Field(gt=0)] | None = None
    last_completed_sha: Sha | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def cursor_fields_are_paired(self) -> EnvironmentState:
        if (self.last_seen_deployment_id is None) != (self.last_seen_sha is None):
            raise ValueError("last_seen deployment ID and SHA must be set together")
        if (self.last_completed_deployment_id is None) != (self.last_completed_sha is None):
            raise ValueError("last_completed deployment ID and SHA must be set together")
        return self


class State(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = SCHEMA_VERSION
    revision: Annotated[int, Field(ge=0)] = 0
    environments: dict[str, EnvironmentState] = Field(default_factory=dict)
    deployments: dict[str, DeploymentEvent] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keys_match_records(self) -> State:
        for key, environment in self.environments.items():
            project_id, canonical_environment = _parse_environment_partition(key)
            if canonical_environment is None:
                raise ValueError("environment keys must use canonical names")
            if environment.last_seen_deployment_id is not None:
                seen = self.deployments.get(
                    deployment_key(project_id, environment.last_seen_deployment_id)
                )
                if seen is None:
                    raise ValueError("last_seen cursor must reference a deployment")
                if seen.environment != canonical_environment:
                    raise ValueError("last_seen cursor environment must match deployment")
                if project_id is not None and deployment_project_id(seen) != project_id:
                    raise ValueError("last_seen cursor project must match deployment")
                if seen.sha != environment.last_seen_sha:
                    raise ValueError("last_seen cursor SHA must match deployment")
            if environment.last_completed_deployment_id is not None:
                completed = self.deployments.get(
                    deployment_key(project_id, environment.last_completed_deployment_id)
                )
                if completed is None:
                    raise ValueError("last_completed cursor must reference a deployment")
                if completed.environment != canonical_environment:
                    raise ValueError("last_completed cursor environment must match deployment")
                if project_id is not None and deployment_project_id(completed) != project_id:
                    raise ValueError("last_completed cursor project must match deployment")
                if completed.sha != environment.last_completed_sha:
                    raise ValueError("last_completed cursor SHA must match deployment")
                if completed.status is not EventStatus.COMPLETED:
                    raise ValueError("last_completed cursor requires completed deployment")
        for key, deployment in self.deployments.items():
            if key != deployment_key(deployment.project_id, deployment.deployment_id):
                raise ValueError("deployment key must match project_id and deployment_id")
            if deployment.environment not in CANONICAL_ENVIRONMENTS:
                raise ValueError("deployment environment must use a canonical name")
        return self

    def ordered_events(
        self, environment: str | None = None, project_id: int | None = None
    ) -> list[DeploymentEvent]:
        selected = (
            event
            for event in self.deployments.values()
            if environment is None or event.environment == environment.lower()
            if project_id is None or deployment_project_id(event) == project_id
        )
        return sorted(
            selected,
            key=lambda item: (item.finished_at, item.project_id, item.deployment_id),
        )


def _parse_environment_partition(key: str) -> tuple[int, str | None]:
    project, separator, environment = key.partition(":")
    if (
        separator
        and project.isdigit()
        and int(project) > 0
        and environment in CANONICAL_ENVIRONMENTS
    ):
        return int(project), environment
    return 0, None
