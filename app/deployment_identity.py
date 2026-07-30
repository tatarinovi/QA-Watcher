from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models import DeploymentEvent, State, deployment_key


class ResolutionCode(StrEnum):
    DEPLOYMENT_NOT_FOUND = "DEPLOYMENT_NOT_FOUND"
    AMBIGUOUS_DEPLOYMENT_ID = "AMBIGUOUS_DEPLOYMENT_ID"


@dataclass(frozen=True)
class DeploymentResolution:
    event: DeploymentEvent | None
    key: str | None
    error: ResolutionCode | None = None


def resolve_deployment(
    state: State, deployment_id: int, project_id: int | None = None
) -> DeploymentResolution:
    if project_id is not None:
        key = deployment_key(project_id, deployment_id)
        event = state.deployments.get(key)
        if event is None:
            return DeploymentResolution(None, None, ResolutionCode.DEPLOYMENT_NOT_FOUND)
        return DeploymentResolution(event, key)
    matches = [
        (key, event)
        for key, event in state.deployments.items()
        if event.deployment_id == deployment_id
    ]
    if not matches:
        return DeploymentResolution(None, None, ResolutionCode.DEPLOYMENT_NOT_FOUND)
    if len(matches) != 1:
        return DeploymentResolution(None, None, ResolutionCode.AMBIGUOUS_DEPLOYMENT_ID)
    key, event = matches[0]
    return DeploymentResolution(event, key)
