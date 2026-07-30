from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.deployment_ingestor import normalize_environment
from app.models import DeploymentEvent, EventStatus, deployment_project_id
from app.storage import EventStore

MAX_PAGE_LIMIT = 100


@dataclass(frozen=True)
class PendingDeployment:
    deployment_id: int
    project_id: int
    environment: str
    previous_sha: str
    sha: str
    finished_at: str
    claimable: bool
    blocked_by_deployment_id: int | None
    blocked_by_project_id: int | None
    status: str


@dataclass(frozen=True)
class PendingPage:
    items: tuple[PendingDeployment, ...]
    next_cursor: str | None


class PendingQuery:
    def __init__(
        self,
        store: EventStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_pending(
        self,
        *,
        environment: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> PendingPage:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
        after = _decode_cursor(cursor) if cursor is not None else None
        normalized_environment = (
            normalize_environment(environment) if environment is not None else None
        )
        now = self._clock()
        unfinished = [
            event
            for event in self._store.load().ordered_events()
            if event.status in {EventStatus.PENDING, EventStatus.CLAIMED}
        ]
        blockers: dict[tuple[int, str], DeploymentEvent] = {}
        for event in unfinished:
            blockers.setdefault(_partition(event), event)
        selected = [
            event
            for event in unfinished
            if (environment is None or event.environment == normalized_environment)
            and (after is None or _order_key(event) > after)
        ]
        window = selected[: limit + 1]
        has_more = len(window) > limit
        page_events = window[:limit]
        items = tuple(
            _pending_item(event, blockers[_partition(event)], now) for event in page_events
        )
        next_cursor = (
            _encode_cursor(_order_key(page_events[-1]))
            if has_more and page_events
            else None
        )
        return PendingPage(items=items, next_cursor=next_cursor)


def _pending_item(
    event: DeploymentEvent, blocker: DeploymentEvent, now: datetime
) -> PendingDeployment:
    expired = (
        event.status is EventStatus.CLAIMED
        and event.claim is not None
        and event.completion_intent is None
        and event.claim.expires_at <= now
    )
    pending = event.status is EventStatus.PENDING or expired
    claimable = event.project_id == blocker.project_id and event.deployment_id == blocker.deployment_id and pending
    return PendingDeployment(
        deployment_id=event.deployment_id,
        project_id=deployment_project_id(event),
        environment=event.environment,
        previous_sha=event.previous_sha or "",
        sha=event.sha,
        finished_at=event.finished_at.isoformat(),
        claimable=claimable,
        blocked_by_deployment_id=None if claimable else blocker.deployment_id,
        blocked_by_project_id=None if claimable else blocker.project_id,
        status=EventStatus.PENDING.value if expired else event.status.value,
    )


def _order_key(event: DeploymentEvent) -> tuple[str, int, int]:
    return event.finished_at.isoformat(), event.project_id, event.deployment_id


def _partition(event: DeploymentEvent) -> tuple[int, str]:
    return deployment_project_id(event), event.environment


def _encode_cursor(key: tuple[str, int, int]) -> str:
    raw = json.dumps(list(key), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not isinstance(value[0], str)
            or isinstance(value[1], bool)
            or not isinstance(value[1], int)
            or isinstance(value[2], bool)
            or not isinstance(value[2], int)
        ):
            raise ValueError
        return value[0], value[1], value[2]
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None
