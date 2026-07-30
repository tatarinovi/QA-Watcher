from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.deployment_ingestor import DeploymentIngestor, normalize_environment
from app.gitlab_client import (
    CommitMetadata,
    CompareResult,
    DeploymentVerification,
    GitLabClient,
    ProjectMetadata,
)
from app.storage import JsonEventStore


class ReplayError(RuntimeError):
    """A secret-safe failure while preparing an isolated historical replay."""


@dataclass(frozen=True)
class ReplaySummary:
    project_id: int
    environment: str
    deployment_ids: tuple[int, ...]
    pending_count: int


class _ReplaySource:
    def __init__(
        self,
        client: GitLabClient,
        candidates: list[dict[str, Any]],
        verifications: dict[int, DeploymentVerification],
    ) -> None:
        self.project_id = client.project_id
        self._client = client
        self._candidates = candidates
        self._verifications = verifications

    def deployments(self, **_kwargs: object) -> list[dict[str, object]]:
        return list(self._candidates)

    def verify_deployment(self, candidate: dict[str, object]) -> DeploymentVerification:
        deployment_id = candidate.get("id")
        if isinstance(deployment_id, bool) or not isinstance(deployment_id, int):
            raise ReplayError("selected deployment ID is malformed")
        return self._verifications[deployment_id]

    def project_metadata(self) -> ProjectMetadata:
        return self._client.project_metadata()

    def commit_metadata(self, sha: str) -> CommitMetadata:
        return self._client.commit_metadata(sha)

    def compare(self, previous_sha: str, sha: str) -> CompareResult:
        return self._client.compare(previous_sha, sha)


def prepare_replay(
    client: GitLabClient,
    store: JsonEventStore,
    *,
    environment: str,
    count: int,
) -> ReplaySummary:
    """Build an isolated FIFO from bounded, factual, historical GitLab deployments."""

    if environment not in {"dev", "test", "stage", "prod"}:
        raise ReplayError("unsupported replay environment")
    if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 20:
        raise ReplayError("replay count must be between 2 and 20")
    if store.load().deployments:
        raise ReplayError("replay state must be empty")

    candidates = client.recent_deployments(environment=environment, limit=100)
    selected: list[dict[str, Any]] = []
    verifications: dict[int, DeploymentVerification] = {}
    for candidate in candidates:
        verification = client.verify_deployment(candidate)
        deployment = verification.deployment
        if (
            not verification.accepted
            or deployment is None
            or normalize_environment(deployment.environment) != environment
        ):
            continue
        if selected:
            previous_id = int(selected[-1]["id"])
            previous = verifications[previous_id].deployment
            if previous is not None and previous.sha == deployment.sha:
                selected[-1] = candidate
                del verifications[previous_id]
                verifications[deployment.deployment_id] = verification
                continue
        selected.append(candidate)
        verifications[deployment.deployment_id] = verification

    if len(selected) < count:
        raise ReplayError(
            f"found {len(selected)} of {count} eligible deployments for replay"
        )
    selected = selected[-count:]
    selected_ids = {int(candidate["id"]) for candidate in selected}
    verifications = {
        deployment_id: verification
        for deployment_id, verification in verifications.items()
        if deployment_id in selected_ids
    }

    result = DeploymentIngestor(
        _ReplaySource(client, selected, verifications), store
    ).poll()
    if result.added != count:
        raise ReplayError("replay ingestion did not add every selected deployment")

    state = store.load()
    pending_count = sum(
        event.status == "pending"
        for event in state.deployments.values()
        if event.project_id == client.project_id and event.environment == environment
    )
    deployment_ids = tuple(int(candidate["id"]) for candidate in selected)
    return ReplaySummary(
        project_id=client.project_id,
        environment=environment,
        deployment_ids=deployment_ids,
        pending_count=pending_count,
    )
