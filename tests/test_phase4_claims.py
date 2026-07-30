from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.claim_service import ClaimCode, ClaimService
from app.models import DeploymentEvent, EventStatus, State
from app.pending_query import PendingQuery
from app.storage import ConcurrentStateUpdateError, EventStore, JsonEventStore

NOW = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def event(
    deployment_id: int,
    *,
    environment: str = "test",
    finished_at: datetime | None = None,
    status: EventStatus = EventStatus.PENDING,
) -> DeploymentEvent:
    return DeploymentEvent(
        project_id=42,
        deployment_id=deployment_id,
        environment=environment,
        sha=f"{deployment_id:040x}",
        previous_sha="a" * 40,
        finished_at=finished_at or NOW,
        discovered_at=NOW,
        status=status,
        pipeline_ref="test",
    )


def store_with(tmp_path: Path, *events: DeploymentEvent) -> JsonEventStore:
    store = JsonEventStore(tmp_path / "state.json")
    store.save(
        State(deployments={f"42:{item.deployment_id}": item for item in events}),
        expected_revision=0,
    )
    return store


@pytest.mark.parametrize("ttl", [True, False, 299, 14401, 300.0, "3600", None])
def test_invalid_claim_ttl_is_a_structured_domain_error(tmp_path: Path, ttl: object) -> None:
    service = ClaimService(store_with(tmp_path, event(321)), clock=Clock())

    result = service.claim_deployment(321, ttl_seconds=ttl)  # type: ignore[arg-type]

    assert result.code is ClaimCode.INVALID_CLAIM_TTL
    assert result.succeeded is False
    assert result.claim_token is None


def test_claim_persists_only_hash_and_result_repr_redacts_token(tmp_path: Path) -> None:
    store = store_with(tmp_path, event(321))

    result = ClaimService(store, clock=Clock()).claim_deployment(321)

    assert result.code is ClaimCode.CLAIMED
    assert result.succeeded is True
    assert result.claim_token is not None
    assert result.expires_at == NOW + timedelta(seconds=3600)
    claim = store.load().deployments["42:321"].claim
    assert claim is not None
    assert claim.token_hash.startswith("sha256:")
    assert result.claim_token not in claim.token_hash
    assert result.claim_token not in repr(result)
    serialized = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert result.claim_token not in serialized
    assert result.claim_token not in json.dumps(result.as_dict())


def test_same_token_renews_from_now_and_wrong_or_missing_token_is_redacted(
    tmp_path: Path,
) -> None:
    clock = Clock()
    service = ClaimService(store_with(tmp_path, event(321)), clock=clock)
    first = service.claim_deployment(321, ttl_seconds=300)
    assert first.claim_token is not None
    clock.now += timedelta(seconds=100)

    renewed = service.claim_deployment(
        321, claim_token=first.claim_token, ttl_seconds=600  # gitleaks:allow
    )
    missing = service.claim_deployment(321)
    wrong = service.claim_deployment(321, claim_token="not-the-token")

    assert renewed.code is ClaimCode.CLAIM_RENEWED
    assert renewed.claim_token == first.claim_token
    assert renewed.expires_at == clock.now + timedelta(seconds=600)
    assert missing.code is ClaimCode.CLAIM_CONFLICT
    assert wrong.code is ClaimCode.CLAIM_CONFLICT
    for result in (missing, wrong):
        assert result.claim_token is None
        assert first.claim_token not in repr(result)


def test_expired_claim_is_reclaimed_with_a_new_token_and_query_projects_pending(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = store_with(tmp_path, event(321))
    first = ClaimService(store, clock=clock).claim_deployment(321, ttl_seconds=300)
    assert first.claim_token is not None
    clock.now += timedelta(seconds=301)

    item = PendingQuery(store, clock=clock).list_pending().items[0]
    reclaimed = ClaimService(store, clock=clock).claim_deployment(321)

    assert item.status == "pending"
    assert item.claimable is True
    assert item.blocked_by_deployment_id is None
    assert reclaimed.code is ClaimCode.CLAIMED
    assert reclaimed.claim_token is not None
    assert reclaimed.claim_token != first.claim_token


def test_fifo_blocks_later_deployments_but_environments_are_independent(
    tmp_path: Path,
) -> None:
    store = store_with(
        tmp_path,
        event(321, finished_at=NOW),
        event(322, finished_at=NOW + timedelta(seconds=1)),
        event(900, environment="stage", finished_at=NOW),
    )
    service = ClaimService(store, clock=Clock())

    blocked = service.claim_deployment(322)
    first = service.claim_deployment(321)
    still_blocked = service.claim_deployment(322)
    independent = service.claim_deployment(900)

    assert blocked.code is ClaimCode.PREDECESSOR_NOT_COMPLETED
    assert blocked.blocked_by_deployment_id == 321
    assert first.code is ClaimCode.CLAIMED
    assert still_blocked.code is ClaimCode.PREDECESSOR_NOT_COMPLETED
    assert still_blocked.blocked_by_deployment_id == 321
    assert independent.code is ClaimCode.CLAIMED


def test_unknown_and_completed_errors_are_deterministic(tmp_path: Path) -> None:
    store = store_with(tmp_path, event(10, status=EventStatus.COMPLETED))
    service = ClaimService(store, clock=Clock())

    assert service.claim_deployment(999).code is ClaimCode.DEPLOYMENT_NOT_FOUND
    assert service.claim_deployment(10).code is ClaimCode.DEPLOYMENT_COMPLETED


def test_concurrent_claims_have_one_success_and_only_its_token_works(
    tmp_path: Path,
) -> None:
    store = store_with(tmp_path, event(321))

    def claim() -> object:
        return ClaimService(JsonEventStore(tmp_path / "state.json"), clock=Clock()).claim_deployment(
            321
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: claim(), range(8)))

    winners = [result for result in results if result.code is ClaimCode.CLAIMED]  # type: ignore[attr-defined]
    assert len(winners) == 1
    token = winners[0].claim_token  # type: ignore[attr-defined]
    assert token is not None
    assert all(
        result.code is ClaimCode.CLAIM_CONFLICT  # type: ignore[attr-defined]
        for result in results
        if result is not winners[0]
    )
    assert (
        ClaimService(store, clock=Clock()).claim_deployment(321, claim_token=token).code
        is ClaimCode.CLAIM_RENEWED
    )
    assert (
        ClaimService(store, clock=Clock())
        .claim_deployment(321, claim_token="loser-token")
        .code
        is ClaimCode.CLAIM_CONFLICT
    )


def test_concurrent_claims_in_different_environments_both_succeed(
    tmp_path: Path,
) -> None:
    store_with(
        tmp_path,
        event(321, environment="test"),
        event(900, environment="stage"),
    )

    def claim(deployment_id: int) -> object:
        return ClaimService(JsonEventStore(tmp_path / "state.json"), clock=Clock()).claim_deployment(
            deployment_id
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [321, 900]))

    assert [result.code for result in results] == [  # type: ignore[attr-defined]
        ClaimCode.CLAIMED,
        ClaimCode.CLAIMED,
    ]
    assert all(result.claim_token is not None for result in results)  # type: ignore[attr-defined]


class AlwaysConflictStore:
    def __init__(self, delegate: JsonEventStore) -> None:
        self.delegate = delegate

    def load(self) -> State:
        return self.delegate.load()

    def save(self, state: State, *, expected_revision: int) -> State:
        raise ConcurrentStateUpdateError("injected perpetual conflict")


def test_claim_returns_storage_error_after_bounded_conflict_retries(tmp_path: Path) -> None:
    delegate = store_with(tmp_path, event(321))
    store: EventStore = AlwaysConflictStore(delegate)

    result = ClaimService(store, clock=Clock()).claim_deployment(321)

    assert result.code is ClaimCode.STORAGE_ERROR
    assert result.succeeded is False
    assert result.claim_token is None
