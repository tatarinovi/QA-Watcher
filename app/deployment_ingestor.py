from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from app.gitlab_client import (
    CommitMetadata,
    CompareResult,
    DeploymentVerification,
    GitLabClient,
    GitLabProtocolError,
    ProjectMetadata,
)
from app.models import (
    ChangedFilePreview,
    CommitSnapshot,
    ComparePreview,
    CompletionReason,
    DeploymentEvent,
    EnvironmentState,
    EventStatus,
    ProjectSnapshot,
    State,
    deployment_key,
    deployment_project_id,
    environment_partition_key,
)
from app.storage import ConcurrentStateUpdateError, EventStore

ENVIRONMENT_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "test": "test",
    "testing": "test",
    "stage": "stage",
    "staging": "stage",
    "prod": "prod",
    "production": "prod",
}
MAX_STATE_RETRIES = 8


class DeploymentConflictError(RuntimeError):
    """A known deployment ID was observed with conflicting immutable facts."""


class GitLabDeploymentSource(Protocol):
    project_id: int

    def deployments(self, **kwargs: object) -> list[dict[str, object]]: ...

    def verify_deployment(
        self, candidate: dict[str, object]
    ) -> DeploymentVerification: ...

    def project_metadata(self) -> ProjectMetadata: ...

    def commit_metadata(self, sha: str) -> CommitMetadata: ...

    def compare(self, previous_sha: str, sha: str) -> CompareResult: ...


@dataclass(frozen=True)
class DeploymentNotification:
    repository: str
    environment: str
    version: str
    description: str


@dataclass(frozen=True)
class PollResult:
    added: int
    rejected: int
    rejection_reasons: tuple[str, ...] = ()
    notifications: tuple[DeploymentNotification, ...] = ()


class DeploymentIngestor:
    def __init__(self, gitlab: GitLabClient | GitLabDeploymentSource, store: EventStore) -> None:
        self._gitlab = cast(GitLabDeploymentSource, gitlab)
        self._store = store

    def poll(self) -> PollResult:
        for _ in range(MAX_STATE_RETRIES):
            try:
                return self._poll_once()
            except ConcurrentStateUpdateError:
                continue
        raise ConcurrentStateUpdateError("deployment ingestion retry budget exhausted")

    def _poll_once(self) -> PollResult:
        candidates = self._gitlab.deployments()
        verified = []
        rejected = 0
        rejection_reasons: list[str] = []
        notifications: list[DeploymentNotification] = []
        for candidate in candidates:
            try:
                verification = self._gitlab.verify_deployment(candidate)
            except (GitLabProtocolError, ValueError, TypeError):
                rejected += 1
                rejection_reasons.append("canonical_verification_failed")
                continue
            if not verification.accepted or verification.deployment is None:
                rejected += 1
                rejection_reasons.append(
                    verification.rejection.value
                    if verification.rejection is not None
                    else "canonical_verification_failed"
                )
                continue
            environment = normalize_environment(verification.deployment.environment)
            if environment is None:
                rejected += 1
                rejection_reasons.append("unsupported_environment")
                continue
            verified.append((verification.deployment, environment))

        verified.sort(
            key=lambda item: (
                datetime.fromisoformat(item[0].finished_at),
                item[0].deployment_id,
            )
        )
        state = self._store.load()
        deployments = dict(state.deployments)
        environments = dict(state.environments)
        preexisting_partitions = set(environments)
        added = 0
        project: ProjectSnapshot | None = None
        project_id = self._gitlab.project_id
        now = datetime.now(UTC)

        for deployment, environment in verified:
            key = deployment_key(project_id, deployment.deployment_id)
            existing = deployments.get(key)
            finished_at = datetime.fromisoformat(deployment.finished_at)
            if existing is not None:
                if (
                    deployment_project_id(existing) != project_id
                    or existing.environment != environment
                    or existing.sha != deployment.sha
                    or existing.finished_at != finished_at
                    or existing.pipeline_id != deployment.pipeline_id
                    or (
                        existing.pipeline_ref is not None
                        and existing.pipeline_ref != deployment.pipeline_ref
                    )
                ):
                    raise DeploymentConflictError(
                        f"deployment {deployment.deployment_id} has conflicting facts"
                    )
                continue

            predecessor = _latest_verified(deployments, environment, project_id)
            if predecessor is not None and (finished_at, deployment.deployment_id) <= (
                predecessor.finished_at,
                predecessor.deployment_id,
            ):
                rejected += 1
                rejection_reasons.append("late_out_of_order")
                continue
            previous_sha = predecessor.sha if predecessor is not None else None
            if project is None:
                project = _project_snapshot(self._gitlab.project_metadata())
                if project.project_id != project_id:
                    raise DeploymentConflictError(
                        "project metadata ID does not match configured project"
                    )
            commit_metadata = self._gitlab.commit_metadata(deployment.sha)
            commit = _commit_snapshot(commit_metadata)
            status = EventStatus.PENDING
            reason = None
            completed_at = None
            preview = None
            if predecessor is None:
                status = EventStatus.COMPLETED
                reason = CompletionReason.INITIAL_BASELINE
                completed_at = now
            elif previous_sha == deployment.sha:
                status = EventStatus.COMPLETED
                reason = CompletionReason.NO_CHANGES
                completed_at = now
            else:
                assert previous_sha is not None
                try:
                    preview = _compare_preview(
                        self._gitlab.compare(previous_sha, deployment.sha)
                    )
                except (GitLabProtocolError, ValueError, TypeError) as exc:
                    preview = _failed_compare(type(exc).__name__)
            event = DeploymentEvent(
                project_id=project_id,
                deployment_id=deployment.deployment_id,
                environment=environment,
                sha=deployment.sha,
                previous_sha=previous_sha,
                finished_at=finished_at,
                discovered_at=now,
                status=status,
                completed_at=completed_at,
                completion_reason=reason,
                pipeline_id=deployment.pipeline_id,
                pipeline_ref=deployment.pipeline_ref,
                project=project,
                commit=commit,
                compare_preview=preview,
            )
            deployments[key] = event
            partition = environment_partition_key(project_id, environment)
            if (
                event.status is EventStatus.PENDING
                and partition in preexisting_partitions
            ):
                notifications.append(
                    DeploymentNotification(
                        repository=project.name,
                        environment=event.environment,
                        version=commit_metadata.merge_request_title or commit_metadata.title,
                        description=commit_metadata.merge_request_description or "—",
                    )
                )
            environments[partition] = EnvironmentState(
                last_seen_deployment_id=event.deployment_id,
                last_seen_sha=event.sha,
                last_completed_deployment_id=(
                    event.deployment_id
                    if event.status is EventStatus.COMPLETED
                    else environments.get(partition, EnvironmentState()).last_completed_deployment_id
                ),
                last_completed_sha=(
                    event.sha
                    if event.status is EventStatus.COMPLETED
                    else environments.get(partition, EnvironmentState()).last_completed_sha
                ),
                updated_at=now,
            )
            added += 1

        if added:
            self._store.save(
                State(
                    revision=state.revision,
                    environments=environments,
                    deployments=deployments,
                ),
                expected_revision=state.revision,
            )
        return PollResult(
            added=added,
            rejected=rejected,
            rejection_reasons=tuple(rejection_reasons),
            notifications=tuple(notifications),
        )


def normalize_environment(value: str) -> str | None:
    return ENVIRONMENT_ALIASES.get(value.strip().casefold())


def _latest_verified(
    deployments: dict[str, DeploymentEvent], environment: str, project_id: int
) -> DeploymentEvent | None:
    matching = [
        event
        for event in deployments.values()
        if event.environment == environment and deployment_project_id(event) == project_id
    ]
    return max(matching, key=lambda event: (event.finished_at, event.deployment_id), default=None)


def _project_snapshot(metadata: ProjectMetadata) -> ProjectSnapshot:
    return ProjectSnapshot(**metadata.model_dump())


def _commit_snapshot(metadata: CommitMetadata) -> CommitSnapshot:
    return CommitSnapshot(
        sha=metadata.sha,
        title=metadata.title,
        author_name=metadata.author_name,
        authored_date=metadata.authored_date,
        web_url=metadata.web_url,
    )


def _compare_preview(result: CompareResult) -> ComparePreview:
    return ComparePreview(
        changed_files=tuple(
            ChangedFilePreview(**item.model_dump()) for item in result.changed_files
        ),
        changed_file_count=result.changed_file_count,
        added_files=result.stats.added_files,
        renamed_files=result.stats.renamed_files,
        deleted_files=result.stats.deleted_files,
        generated_files=result.stats.generated_files,
        files_truncated=result.files_truncated,
        compare_timeout=result.compare_timeout,
        incomplete=result.incomplete,
        same_ref=result.same_ref,
        failure=result.failure[:128] if result.failure else None,
    )


def _failed_compare(reason: str) -> ComparePreview:
    return ComparePreview(
        changed_files=(),
        changed_file_count=0,
        added_files=0,
        renamed_files=0,
        deleted_files=0,
        generated_files=0,
        files_truncated=False,
        compare_timeout=False,
        incomplete=True,
        same_ref=False,
        failure=reason[:128],
    )
