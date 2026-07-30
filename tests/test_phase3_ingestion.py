from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.claim_service import ClaimService
from app.deployment_ingestor import DeploymentConflictError, DeploymentIngestor
from app.gitlab_client import (
    ChangedFile,
    CommitMetadata,
    CompareResult,
    CompareStats,
    DeploymentVerification,
    GitLabProtocolError,
    ProjectMetadata,
    VerifiedDeployment,
)
from app.models import CompletionReason, EventStatus, State
from app.pending_query import PendingQuery
from app.scheduler import NotifyingPoller
from app.storage import ConcurrentStateUpdateError, EventStore, JsonEventStore


class FakeGitLab:
    def __init__(
        self,
        deployments: list[dict[str, Any]],
        *,
        compare_failure: bool = False,
        project_id: int = 42,
    ) -> None:
        self.candidates = deployments
        self.compare_failure = compare_failure
        self.project_id = project_id
        self.compare_calls: list[tuple[str, str]] = []

    def deployments(self, **_: object) -> list[dict[str, Any]]:
        return self.candidates

    def verify_deployment(self, candidate: dict[str, Any]) -> DeploymentVerification:
        if candidate.get("invalid"):
            raise GitLabProtocolError("unsafe fixture detail")
        return DeploymentVerification(
            deployment_id=int(candidate["id"]),
            accepted=True,
            deployment=VerifiedDeployment(
                deployment_id=int(candidate["id"]),
                environment=str(candidate["environment"]),
                sha=str(candidate["sha"]),
                finished_at=str(candidate["finished_at"]),
                pipeline_id=int(candidate["pipeline_id"]),
                pipeline_ref="test",
            ),
        )

    def project_metadata(self) -> ProjectMetadata:
        return ProjectMetadata(
            gitlab_host="gitlab.example",
            project_id=self.project_id,
            name=f"watcher-{self.project_id}",
            web_url=f"https://gitlab.example/team/watcher-{self.project_id}",
            http_repo_url=f"https://gitlab.example/team/watcher-{self.project_id}.git",
            ssh_repo_url=f"ssh://gitlab.example/team/watcher-{self.project_id}.git",
        )

    def commit_metadata(self, sha: str) -> CommitMetadata:
        return CommitMetadata(
            sha=sha,
            title=f"commit {sha}",
            author_name="QA",
            authored_date="2026-07-29T10:00:00Z",
            web_url=f"https://gitlab.example/team/watcher/-/commit/{sha}",
            merge_request_title=f"MR {sha}",
            merge_request_description=f"Description {sha}",
        )

    def compare(self, previous_sha: str, sha: str) -> CompareResult:
        self.compare_calls.append((previous_sha, sha))
        if self.compare_failure:
            return CompareResult(
                old_sha=previous_sha,
                new_sha=sha,
                changed_files=(),
                changed_file_count=0,
                stats=CompareStats(
                    added_files=0,
                    renamed_files=0,
                    deleted_files=0,
                    generated_files=0,
                ),
                files_truncated=False,
                compare_timeout=True,
                incomplete=True,
                same_ref=False,
                failure="transport_ReadTimeout",
            )
        return CompareResult(
            old_sha=previous_sha,
            new_sha=sha,
            changed_files=(
                ChangedFile(
                    old_path="old.py",
                    new_path="new.py",
                    added=False,
                    renamed=True,
                    deleted=False,
                    generated=False,
                    collapsed=False,
                    too_large=False,
                ),
            ),
            changed_file_count=1,
            stats=CompareStats(
                added_files=0,
                renamed_files=1,
                deleted_files=0,
                generated_files=0,
            ),
            files_truncated=False,
            compare_timeout=False,
            incomplete=False,
            same_ref=False,
        )


class MetadataUnavailableGitLab(FakeGitLab):
    def project_metadata(self) -> ProjectMetadata:
        raise AssertionError("steady-state poll must not request project metadata")


def candidate(
    deployment_id: int,
    sha: str,
    *,
    environment: str = "TESTING",
    invalid: bool = False,
) -> dict[str, Any]:
    return {
        "id": deployment_id,
        "environment": environment,
        "sha": sha,
        "pipeline_id": deployment_id + 1000,
        "finished_at": f"2026-07-29T10:{deployment_id % 60:02d}:00Z",
        "invalid": invalid,
    }


def test_notifying_poller_ignores_baseline_and_sends_merge_request_message(
    tmp_path: Path,
) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab([candidate(320, "320")])
    messages: list[tuple[str, str, str, str]] = []

    class Notifier:
        def deployment_detected(
            self,
            *,
            repository: str,
            environment: str,
            version: str,
            description: str,
        ) -> None:
            messages.append((repository, environment, version, description))

    poller = NotifyingPoller(DeploymentIngestor(gitlab, store), Notifier())

    assert poller.poll().added == 1
    assert messages == []

    gitlab.candidates.append(candidate(321, "321"))
    assert poller.poll().added == 1
    assert messages == [
        ("watcher-42", "test", "MR 321", "Description 321")
    ]


def test_notifying_poller_suppresses_initial_history_backfill(tmp_path: Path) -> None:
    messages: list[str] = []

    class Notifier:
        def deployment_detected(
            self,
            *,
            repository: str,
            environment: str,
            version: str,
            description: str,
        ) -> None:
            messages.append(version)

    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab([candidate(320, "320"), candidate(321, "321")])
    poller = NotifyingPoller(DeploymentIngestor(gitlab, store), Notifier())

    result = poller.poll()

    assert result.added == 2
    assert messages == []
    assert store.load().deployments["42:321"].status == "pending"


def test_telegram_failure_does_not_rollback_durable_ingestion(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab([candidate(320, "320")])

    class FailedNotifier:
        def deployment_detected(
            self,
            *,
            repository: str,
            environment: str,
            version: str,
            description: str,
        ) -> None:
            raise RuntimeError("sensitive Telegram failure")

    poller = NotifyingPoller(DeploymentIngestor(gitlab, store), FailedNotifier())
    poller.poll()
    gitlab.candidates.append(candidate(321, "321"))

    result = poller.poll()

    assert result.added == 1
    assert store.load().deployments["42:321"].status == "pending"


def test_notifying_poller_falls_back_to_commit_when_exact_merge_request_is_absent(
    tmp_path: Path,
) -> None:
    class CommitOnlyGitLab(FakeGitLab):
        def commit_metadata(self, sha: str) -> CommitMetadata:
            return CommitMetadata(
                sha=sha,
                title=f"commit {sha}",
                author_name="QA",
                authored_date="2026-07-29T10:00:00Z",
                web_url=f"https://gitlab.example/commit/{sha}",
            )

    messages: list[tuple[str, str]] = []

    class Notifier:
        def deployment_detected(
            self,
            *,
            repository: str,
            environment: str,
            version: str,
            description: str,
        ) -> None:
            messages.append((version, description))

    store = JsonEventStore(tmp_path / "state.json")
    gitlab = CommitOnlyGitLab([candidate(320, "320")])
    poller = NotifyingPoller(DeploymentIngestor(gitlab, store), Notifier())
    poller.poll()
    gitlab.candidates.append(candidate(321, "321"))

    poller.poll()

    assert messages == [("commit 321", "—")]


def test_restart_durability_fifo_exact_pairs_and_duplicate_poll(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    gitlab = FakeGitLab(
        [candidate(320, "320"), candidate(321, "321"), candidate(322, "322")]
    )
    first = DeploymentIngestor(gitlab, JsonEventStore(path))
    result = first.poll()
    revision = JsonEventStore(path).load().revision

    restarted = PendingQuery(JsonEventStore(path))
    page = restarted.list_pending(environment="test", limit=10)
    assert result.added == 3
    assert [(item.previous_sha, item.sha) for item in page.items] == [
        ("320", "321"),
        ("321", "322"),
    ]
    assert [item.deployment_id for item in page.items] == [321, 322]
    assert page.items[0].claimable is True
    assert page.items[1].claimable is False
    assert page.items[1].blocked_by_deployment_id == 321
    assert ("320", "322") not in gitlab.compare_calls

    assert DeploymentIngestor(gitlab, JsonEventStore(path)).poll().added == 0
    assert JsonEventStore(path).load().revision == revision


def test_baseline_rollback_same_sha_and_invalid_candidate_chain(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab(
        [
            candidate(1, "aaa", environment="Development"),
            candidate(2, "bad", environment="unknown", invalid=True),
            candidate(3, "aaa", environment="DEV"),
            candidate(4, "bbb", environment="dev"),
            candidate(5, "aaa", environment="dev"),
        ]
    )
    result = DeploymentIngestor(gitlab, store).poll()
    state = store.load()

    assert result.rejected == 1
    assert state.deployments["42:1"].completion_reason is CompletionReason.INITIAL_BASELINE
    assert state.deployments["42:3"].completion_reason is CompletionReason.NO_CHANGES
    assert state.deployments["42:4"].previous_sha == "aaa"
    assert state.deployments["42:5"].previous_sha == "bbb"
    assert state.deployments["42:4"].status is EventStatus.PENDING
    assert state.deployments["42:5"].status is EventStatus.PENDING


def test_invalid_environment_rejected_after_verification(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab([candidate(1, "aaa", environment="review/123")])

    result = DeploymentIngestor(gitlab, store).poll()

    assert result.added == 0
    assert result.rejected == 1
    assert store.load().deployments == {}


def test_compare_failure_is_durable_bounded_and_contains_no_raw_diff(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab(
        [candidate(1, "aaa"), candidate(2, "bbb")],
        compare_failure=True,
    )
    DeploymentIngestor(gitlab, store).poll()

    event = store.load().deployments["42:2"]
    assert event.compare_preview is not None
    assert event.compare_preview.incomplete is True
    assert event.compare_preview.failure == "transport_ReadTimeout"
    assert "diff" not in event.model_dump_json().lower()
    assert event.project is not None
    assert event.commit is not None
    assert event.pipeline_id == 1002


def test_conflicting_duplicate_id_fails_closed(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    DeploymentIngestor(FakeGitLab([candidate(1, "aaa")]), store).poll()

    with pytest.raises(DeploymentConflictError):
        DeploymentIngestor(FakeGitLab([candidate(1, "changed")]), store).poll()

    assert store.load().deployments["42:1"].sha == "aaa"


def test_steady_state_duplicate_does_not_fetch_project_metadata(tmp_path: Path) -> None:
    deployment = candidate(1, "aaa")
    store = JsonEventStore(tmp_path / "state.json")
    DeploymentIngestor(FakeGitLab([deployment]), store).poll()

    result = DeploymentIngestor(MetadataUnavailableGitLab([deployment]), store).poll()

    assert result.added == 0
    assert result.rejected == 0


def test_migrated_event_with_unknown_pipeline_ref_remains_idempotent(tmp_path: Path) -> None:
    deployment = candidate(1, "aaa")
    store = JsonEventStore(tmp_path / "state.json")
    DeploymentIngestor(FakeGitLab([deployment]), store).poll()
    state = store.load()
    existing = state.deployments["42:1"]
    store._write_unlocked(
        state.model_copy(
            update={
                "deployments": {
                    "42:1": existing.model_copy(update={"pipeline_ref": None})
                }
            }
        )
    )

    result = DeploymentIngestor(FakeGitLab([deployment]), store).poll()

    assert result.added == 0
    assert result.rejected == 0


def test_late_older_deployment_cannot_rewire_immutable_chain_or_regress_cursor(
    tmp_path: Path,
) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    first = FakeGitLab([candidate(1, "aaa"), candidate(3, "ccc")])
    DeploymentIngestor(first, store).poll()

    late = candidate(2, "bbb")
    result = DeploymentIngestor(FakeGitLab([late]), store).poll()
    state = store.load()

    assert result.added == 0
    assert result.rejection_reasons == ("late_out_of_order",)
    assert "2" not in state.deployments
    assert state.deployments["42:3"].previous_sha == "aaa"
    assert state.environments["42:test"].last_seen_deployment_id == 3


def test_pending_cursor_pagination_and_independent_environments(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    gitlab = FakeGitLab(
        [
            candidate(1, "t1", environment="test"),
            candidate(2, "s1", environment="staging"),
            candidate(3, "t2", environment="test"),
            candidate(4, "t3", environment="test"),
            candidate(5, "s2", environment="stage"),
        ]
    )
    DeploymentIngestor(gitlab, store).poll()
    query = PendingQuery(store)

    first = query.list_pending(limit=1)
    second = query.list_pending(limit=1, cursor=first.next_cursor)
    rest = query.list_pending(limit=10, cursor=second.next_cursor)
    items = [*first.items, *second.items, *rest.items]

    assert [item.deployment_id for item in items] == [3, 4, 5]
    assert {item.deployment_id for item in items if item.claimable} == {3, 5}
    with pytest.raises(ValueError, match="limit"):
        query.list_pending(limit=0)
    with pytest.raises(ValueError, match="cursor"):
        query.list_pending(limit=2, cursor="not-opaque")
    with pytest.raises(ValueError, match="cursor"):
        query.list_pending(limit=2, cursor="a")


def test_projects_have_independent_baselines_and_environment_fifo(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    DeploymentIngestor(
        FakeGitLab([candidate(1, "a1"), candidate(2, "a2")], project_id=42),
        store,
    ).poll()
    DeploymentIngestor(
        FakeGitLab([candidate(3, "b1"), candidate(4, "b2")], project_id=43),
        store,
    ).poll()

    state = store.load()
    pending = PendingQuery(store).list_pending(limit=10).items

    assert state.deployments["42:1"].completion_reason is CompletionReason.INITIAL_BASELINE
    assert state.deployments["43:3"].completion_reason is CompletionReason.INITIAL_BASELINE
    assert state.deployments["42:2"].previous_sha == "a1"
    assert state.deployments["43:4"].previous_sha == "b1"
    assert set(state.environments) >= {"42:test", "43:test"}
    assert [(item.project_id, item.deployment_id, item.claimable) for item in pending] == [
        (42, 2, True),
        (43, 4, True),
    ]
    assert ClaimService(store).claim_deployment(2).succeeded is True
    assert ClaimService(store).claim_deployment(4).succeeded is True


def test_same_deployment_id_from_another_project_coexists(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    DeploymentIngestor(
        FakeGitLab([candidate(1, "a1")], project_id=42), store
    ).poll()

    DeploymentIngestor(FakeGitLab([candidate(1, "b1")], project_id=43), store).poll()

    assert set(store.load().deployments) == {"42:1", "43:1"}


class OneConflictStore:
    def __init__(self, delegate: JsonEventStore) -> None:
        self.delegate = delegate
        self.conflicts_remaining = 1

    def load(self) -> State:
        return self.delegate.load()

    def save(self, state: State, *, expected_revision: int) -> State:
        if self.conflicts_remaining:
            self.conflicts_remaining -= 1
            current = self.delegate.load()
            self.delegate.save(current, expected_revision=current.revision)
            raise ConcurrentStateUpdateError("injected concurrent update")
        return self.delegate.save(state, expected_revision=expected_revision)


def test_ingestor_retries_optimistic_conflict_without_losing_events(
    tmp_path: Path,
) -> None:
    delegate = JsonEventStore(tmp_path / "state.json")
    store: EventStore = OneConflictStore(delegate)

    result = DeploymentIngestor(
        FakeGitLab([candidate(1, "aaa"), candidate(2, "bbb")]),
        store,
    ).poll()

    assert result.added == 2
    assert set(delegate.load().deployments) == {"42:1", "42:2"}
