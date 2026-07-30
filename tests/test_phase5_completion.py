from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.claim_service import ClaimCode, ClaimService
from app.completion_service import CompletionCode, CompletionService
from app.models import DeploymentEvent, EventStatus, State
from app.pending_query import PendingQuery
from app.report_service import ReportCode, ReportService
from app.storage import ImmutableDeploymentError, JsonEventStore

NOW = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
HEADINGS = (
    "# QA Impact Analysis",
    "## Summary",
    "## Changed Areas",
    "## Risk Analysis",
    "## Recommended Checks",
    "## Regression Focus",
)
REPORT = "\n\n".join((*HEADINGS, "done")) + "\n"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def claimed(tmp_path: Path) -> tuple[JsonEventStore, str, Clock]:
    store = JsonEventStore(tmp_path / "state.json")
    event = DeploymentEvent(
        project_id=42,
        deployment_id=321,
        environment="test",
        sha="b" * 40,
        previous_sha="a" * 40,
        finished_at=NOW,
        discovered_at=NOW,
        pipeline_ref="test",
    )
    store.save(State(deployments={"42:321": event}), expected_revision=0)
    clock = Clock()
    result = ClaimService(store, clock=clock).claim_deployment(321)
    assert result.claim_token is not None
    return store, result.claim_token, clock


def test_composite_deployments_claim_complete_and_report_independently(
    tmp_path: Path,
) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    events = {
        f"{project_id}:77": DeploymentEvent(
            project_id=project_id,
            deployment_id=77,
            environment="test",
            sha=f"{project_id:040x}",
            previous_sha="a" * 40,
            finished_at=NOW,
            discovered_at=NOW,
            pipeline_ref="test",
        )
        for project_id in (42, 43)
    }
    store.save(State(deployments=events), expected_revision=0)
    claims = ClaimService(store, clock=Clock())

    assert claims.claim_deployment(77).code is ClaimCode.AMBIGUOUS_DEPLOYMENT_ID
    token_42 = claims.claim_deployment(77, project_id=42).claim_token
    token_43 = claims.claim_deployment(77, project_id=43).claim_token
    assert token_42 is not None and token_43 is not None

    completion = CompletionService(store, tmp_path / "reports", clock=Clock())
    result_42 = completion.mark_analysis_completed(
        77, token_42, REPORT, project_id=42
    )
    result_43 = completion.mark_analysis_completed(
        77, token_43, REPORT + "\nproject 43\n", project_id=43
    )
    assert result_42.succeeded and result_43.succeeded
    assert result_42.report_path is not None
    assert result_42.report_path.startswith("projects/42/test/")
    assert result_43.report_path is not None
    assert result_43.report_path.startswith("projects/43/test/")

    reports = ReportService(store, tmp_path / "reports")
    assert reports.get_report(77).code is ReportCode.AMBIGUOUS_DEPLOYMENT_ID
    assert reports.get_report(77, project_id=42).content == REPORT
    assert reports.get_report(77, project_id=43).content == REPORT + "\nproject 43\n"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("", CompletionCode.REPORT_EMPTY),
        (" \n\t", CompletionCode.REPORT_EMPTY),
        ("\ud800", CompletionCode.REPORT_INVALID_STRUCTURE),
        ("x" * 100001, CompletionCode.REPORT_TOO_LARGE),
        (REPORT.replace("## Summary", ""), CompletionCode.REPORT_INVALID_STRUCTURE),
        (REPORT.replace("## Summary", "## Risk Analysis"), CompletionCode.REPORT_INVALID_STRUCTURE),
        (
            REPORT.replace("## Summary", "## Regression Focus\n## Summary"),
            CompletionCode.REPORT_INVALID_STRUCTURE,
        ),
        (REPORT + "\n## Summary\n", CompletionCode.REPORT_INVALID_STRUCTURE),
    ],
)
def test_validation_happens_before_side_effects(
    tmp_path: Path, body: str, code: CompletionCode
) -> None:
    store, token, clock = claimed(tmp_path)
    before = (tmp_path / "state.json").read_bytes()

    result = CompletionService(store, tmp_path / "reports", clock=clock).mark_analysis_completed(
        321, token, body, "complete"
    )

    assert result.code is code
    assert (tmp_path / "state.json").read_bytes() == before
    assert not (tmp_path / "reports").exists()


def test_multibyte_byte_limit_and_outcome_validation(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)
    service = CompletionService(store, tmp_path / "reports", clock=clock)
    prefix = REPORT + "\n"
    oversized = prefix + "😀" * ((512001 - len(prefix.encode())) // 4 + 1)

    assert service.mark_analysis_completed(321, token, oversized, "complete").code is (
        CompletionCode.REPORT_TOO_LARGE
    )
    assert service.mark_analysis_completed(321, token, REPORT, "other").code is (
        CompletionCode.INVALID_ANALYSIS_OUTCOME
    )


def test_completion_receipt_cursor_and_report_chunks(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)
    result = CompletionService(store, tmp_path / "reports", clock=clock).mark_analysis_completed(
        321, token, REPORT, "partial"
    )

    assert result.code is CompletionCode.COMPLETED
    event = store.load().deployments["42:321"]
    assert event.status is EventStatus.COMPLETED
    assert event.claim is None and event.completion_intent is None
    assert event.completion_receipt is not None
    assert event.completion_receipt.analysis_outcome == "partial"
    assert store.load().environments["42:test"].last_completed_deployment_id == 321
    first = ReportService(store, tmp_path / "reports").get_report(321, max_chars=10)
    assert first.code is ReportCode.REPORT_AVAILABLE
    assert first.content == REPORT[:10]
    assert first.has_more and first.next_cursor is not None
    second = ReportService(store, tmp_path / "reports").get_report(
        321, cursor=first.next_cursor, max_chars=20000
    )
    assert second.content == REPORT[10:]
    assert not second.has_more
    assert first.sha256 == hashlib.sha256(REPORT.encode()).hexdigest()


@pytest.mark.parametrize("point", ["after_intent", "after_artifact"])
def test_crash_is_reconciled_without_exposing_provisional_report(
    tmp_path: Path, point: str
) -> None:
    store, token, clock = claimed(tmp_path)

    def crash(stage: str) -> None:
        if stage == point:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected"):
        CompletionService(
            store, tmp_path / "reports", clock=clock, crash_injector=crash
        ).mark_analysis_completed(321, token, REPORT, "complete")
    assert ReportService(store, tmp_path / "reports").get_report(321).code is (
        ReportCode.REPORT_NOT_AVAILABLE
    )

    retry = CompletionService(store, tmp_path / "reports", clock=clock)
    result = retry.mark_analysis_completed(321, token, REPORT, "complete")
    if point == "after_intent":
        assert result.code is CompletionCode.COMPLETED
    else:
        assert result.code is CompletionCode.COMPLETED


def test_reconciliation_ignores_expired_lease_and_rejects_mismatch(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)

    def crash(stage: str) -> None:
        if stage == "after_artifact":
            raise RuntimeError

    with pytest.raises(RuntimeError):
        CompletionService(
            store, tmp_path / "reports", clock=clock, crash_injector=crash
        ).mark_analysis_completed(321, token, REPORT, "complete")
    event = store.load().deployments["42:321"]
    assert event.completion_intent is not None
    path = tmp_path / "reports" / event.completion_intent.report_path
    path.write_text("tampered", encoding="utf-8")
    clock.now += timedelta(hours=2)

    assert CompletionService(store, tmp_path / "reports", clock=clock).reconcile().code is (
        CompletionCode.STORAGE_ERROR
    )
    assert store.load().deployments["42:321"].status is EventStatus.CLAIMED
    assert ReportService(store, tmp_path / "reports").get_report(321).code is (
        ReportCode.REPORT_NOT_AVAILABLE
    )


def test_expired_claim_with_intent_cannot_be_reclaimed_or_projected_pending(
    tmp_path: Path,
) -> None:
    store, token, clock = claimed(tmp_path)

    def crash(stage: str) -> None:
        if stage == "after_intent":
            raise RuntimeError

    with pytest.raises(RuntimeError):
        CompletionService(
            store, tmp_path / "reports", clock=clock, crash_injector=crash
        ).mark_analysis_completed(321, token, REPORT, "complete")
    clock.now += timedelta(hours=2)

    item = PendingQuery(store, clock=clock).list_pending().items[0]
    result = ClaimService(store, clock=clock).claim_deployment(321)

    assert item.status == "claimed"
    assert item.claimable is False
    assert result.code is ClaimCode.CLAIM_CONFLICT
    assert store.load().deployments["42:321"].completion_intent is not None


def test_idempotency_claim_errors_and_redaction(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)
    service = CompletionService(store, tmp_path / "reports", clock=clock)
    first = service.mark_analysis_completed(321, token, REPORT, "complete")

    assert service.mark_analysis_completed(321, token, REPORT, "complete") == first
    assert service.mark_analysis_completed(321, token, REPORT + "x", "complete").code is (
        CompletionCode.COMPLETION_PAYLOAD_MISMATCH
    )
    assert service.mark_analysis_completed(321, token, REPORT, "partial").code is (
        CompletionCode.COMPLETION_PAYLOAD_MISMATCH
    )
    assert service.mark_analysis_completed(321, "wrong", REPORT, "complete").code is (
        CompletionCode.ALREADY_COMPLETED
    )
    assert token not in repr(first)
    assert token not in (tmp_path / "state.json").read_text()


def test_expired_and_wrong_claim_codes(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)
    service = CompletionService(store, tmp_path / "reports", clock=clock)
    assert service.mark_analysis_completed(321, "wrong", REPORT, "complete").code is (
        CompletionCode.CLAIM_TOKEN_INVALID
    )
    clock.now += timedelta(hours=2)
    assert service.mark_analysis_completed(321, token, REPORT, "complete").code is (
        CompletionCode.CLAIM_EXPIRED
    )


def test_concurrent_completion_has_one_receipt_and_idempotent_results(tmp_path: Path) -> None:
    _, token, clock = claimed(tmp_path)

    def complete() -> CompletionCode:
        return CompletionService(
            JsonEventStore(tmp_path / "state.json"), tmp_path / "reports", clock=clock
        ).mark_analysis_completed(321, token, REPORT, "complete").code

    with ThreadPoolExecutor(max_workers=6) as executor:
        codes = list(executor.map(lambda _: complete(), range(6)))
    assert set(codes) == {CompletionCode.COMPLETED}


def test_completion_pins_unique_project_identity_before_durable_finalize(
    tmp_path: Path,
) -> None:
    store, token, clock = claimed(tmp_path)

    def add_colliding_project(stage: str) -> None:
        if stage != "after_intent":
            return
        state = store.load()
        second = DeploymentEvent(
            project_id=43,
            deployment_id=321,
            environment="test",
            sha="c" * 40,
            previous_sha="a" * 40,
            finished_at=NOW,
            discovered_at=NOW,
            pipeline_ref="test",
        )
        store.save(
            state.model_copy(
                update={"deployments": {**state.deployments, "43:321": second}}
            ),
            expected_revision=state.revision,
        )

    result = CompletionService(
        store,
        tmp_path / "reports",
        clock=clock,
        crash_injector=add_colliding_project,
    ).mark_analysis_completed(321, token, REPORT)

    assert result.code is CompletionCode.COMPLETED
    assert result.project_id == 42
    assert store.load().deployments["42:321"].status is EventStatus.COMPLETED
    assert store.load().deployments["43:321"].status is EventStatus.PENDING


def test_report_detects_artifact_corruption_and_validates_cursor(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)
    CompletionService(store, tmp_path / "reports", clock=clock).mark_analysis_completed(
        321, token, REPORT, "complete"
    )
    event = store.load().deployments["42:321"]
    assert event.completion_receipt is not None
    (tmp_path / "reports" / event.completion_receipt.report_path).write_text(
        "bad", encoding="utf-8"
    )
    service = ReportService(store, tmp_path / "reports")
    assert service.get_report(321).code is ReportCode.STORAGE_ERROR
    assert service.get_report(321, cursor="not-opaque").code is ReportCode.INVALID_CURSOR
    assert service.get_report(321, max_chars=0).code is ReportCode.INVALID_MAX_CHARS


def test_completed_receipt_is_terminal_and_cannot_be_rewritten(tmp_path: Path) -> None:
    store, token, clock = claimed(tmp_path)
    CompletionService(store, tmp_path / "reports", clock=clock).mark_analysis_completed(
        321, token, REPORT, "complete"
    )
    state = store.load()
    completed = state.deployments["42:321"]
    assert completed.completion_receipt is not None
    tampered_receipt = completed.completion_receipt.model_copy(
        update={"report_sha256": "0" * 64}
    )
    deployments = dict(state.deployments)
    deployments["42:321"] = completed.model_copy(
        update={"completion_receipt": tampered_receipt}
    )

    with pytest.raises(ImmutableDeploymentError):
        store.save(
            state.model_copy(update={"deployments": deployments}),
            expected_revision=state.revision,
        )
