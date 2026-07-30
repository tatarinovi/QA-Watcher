from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.deployment_identity import ResolutionCode, resolve_deployment
from app.models import Claim, DeploymentEvent, EventStatus, State
from app.storage import ConcurrentStateUpdateError, EventStore

MIN_CLAIM_TTL = 300
DEFAULT_CLAIM_TTL = 3600
MAX_CLAIM_TTL = 14400
MAX_STATE_RETRIES = 8


class ClaimCode(StrEnum):
    CLAIMED = "CLAIMED"
    CLAIM_RENEWED = "CLAIM_RENEWED"
    INVALID_CLAIM_TTL = "INVALID_CLAIM_TTL"
    DEPLOYMENT_NOT_FOUND = "DEPLOYMENT_NOT_FOUND"
    DEPLOYMENT_COMPLETED = "DEPLOYMENT_COMPLETED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    CLAIM_CONFLICT = "CLAIM_CONFLICT"
    PREDECESSOR_NOT_COMPLETED = "PREDECESSOR_NOT_COMPLETED"
    STORAGE_ERROR = "STORAGE_ERROR"
    AMBIGUOUS_DEPLOYMENT_ID = "AMBIGUOUS_DEPLOYMENT_ID"


@dataclass(frozen=True)
class ClaimResult:
    code: ClaimCode
    deployment_id: int
    succeeded: bool = False
    project_id: int | None = None
    expires_at: datetime | None = None
    blocked_by_deployment_id: int | None = None
    blocked_by_project_id: int | None = None
    claim_token: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Return the non-secret result envelope suitable for logs or reports."""
        return {
            "code": self.code.value,
            "deployment_id": self.deployment_id,
            "project_id": self.project_id,
            "succeeded": self.succeeded,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "blocked_by_deployment_id": self.blocked_by_deployment_id,
            "blocked_by_project_id": self.blocked_by_project_id,
        }


class ClaimService:
    def __init__(
        self,
        store: EventStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def claim_deployment(
        self,
        deployment_id: int,
        *,
        claim_token: str | None = None,
        ttl_seconds: int = DEFAULT_CLAIM_TTL,
        project_id: int | None = None,
    ) -> ClaimResult:
        if not _valid_ttl(ttl_seconds):
            return ClaimResult(
                code=ClaimCode.INVALID_CLAIM_TTL,
                deployment_id=deployment_id,
            )

        for _ in range(MAX_STATE_RETRIES):
            state = self._store.load()
            now = self._clock()
            resolution = resolve_deployment(state, deployment_id, project_id)
            if resolution.error is not None:
                code = (
                    ClaimCode.AMBIGUOUS_DEPLOYMENT_ID
                    if resolution.error is ResolutionCode.AMBIGUOUS_DEPLOYMENT_ID
                    else ClaimCode.DEPLOYMENT_NOT_FOUND
                )
                return ClaimResult(code, deployment_id, project_id=project_id)
            current = resolution.event
            conflict = _domain_conflict(
                state,
                current,
                deployment_id=deployment_id,
                claim_token=claim_token,
                now=now,
            )
            if conflict is not None:
                return conflict

            assert current is not None
            renewing = (
                current.status is EventStatus.CLAIMED
                and current.claim is not None
                and current.claim.expires_at > now
            )
            token = claim_token if renewing else secrets.token_urlsafe(32)
            assert token is not None
            expires_at = now + timedelta(seconds=ttl_seconds)
            replacement = current.model_copy(
                update={
                    "status": EventStatus.CLAIMED,
                    "claim": Claim(
                        token_hash=_token_hash(token),
                        claimed_at=now,
                        expires_at=expires_at,
                    ),
                }
            )
            deployments = dict(state.deployments)
            assert resolution.key is not None
            deployments[resolution.key] = replacement
            try:
                self._store.save(
                    State(
                        revision=state.revision,
                        environments=state.environments,
                        deployments=deployments,
                    ),
                    expected_revision=state.revision,
                )
            except ConcurrentStateUpdateError:
                continue
            return ClaimResult(
                code=ClaimCode.CLAIM_RENEWED if renewing else ClaimCode.CLAIMED,
                deployment_id=deployment_id,
                succeeded=True,
                project_id=current.project_id,
                expires_at=expires_at,
                claim_token=token,
            )
        return ClaimResult(ClaimCode.STORAGE_ERROR, deployment_id)


def _domain_conflict(
    state: State,
    event: DeploymentEvent | None,
    *,
    deployment_id: int,
    claim_token: str | None,
    now: datetime,
) -> ClaimResult | None:
    if event is None:
        return ClaimResult(ClaimCode.DEPLOYMENT_NOT_FOUND, deployment_id)
    if event.status is EventStatus.COMPLETED:
        return ClaimResult(ClaimCode.DEPLOYMENT_COMPLETED, deployment_id)
    if event.completion_intent is not None:
        return ClaimResult(ClaimCode.CLAIM_CONFLICT, deployment_id)

    if (
        event.status is EventStatus.CLAIMED
        and event.claim is not None
        and event.claim.expires_at > now
    ):
        if claim_token is None:
            return ClaimResult(ClaimCode.CLAIM_CONFLICT, deployment_id)
        if not _token_matches(claim_token, event.claim.token_hash):
            return ClaimResult(ClaimCode.CLAIM_CONFLICT, deployment_id)

    oldest = next(
        (
            candidate
            for candidate in state.ordered_events(
                event.environment, event.project_id
            )
            if candidate.status is not EventStatus.COMPLETED
        ),
        None,
    )
    if oldest is not None and oldest.deployment_id != deployment_id:
        return ClaimResult(
            ClaimCode.PREDECESSOR_NOT_COMPLETED,
            deployment_id,
            blocked_by_deployment_id=oldest.deployment_id,
            blocked_by_project_id=oldest.project_id,
        )
    return None


def _valid_ttl(ttl_seconds: object) -> bool:
    return (
        isinstance(ttl_seconds, int)
        and not isinstance(ttl_seconds, bool)
        and MIN_CLAIM_TTL <= ttl_seconds <= MAX_CLAIM_TTL
    )


def _token_hash(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"


def _token_matches(token: str, persisted_hash: str) -> bool:
    return hmac.compare_digest(_token_hash(token), persisted_hash)
