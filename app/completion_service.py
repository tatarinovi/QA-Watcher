from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.deployment_identity import ResolutionCode, resolve_deployment
from app.models import (
    CompletionIntent,
    CompletionReceipt,
    DeploymentEvent,
    EnvironmentState,
    EventStatus,
    deployment_key,
    environment_partition_key,
)
from app.storage import ConcurrentStateUpdateError, EventStore, StateStoreError

MAX_REPORT_CHARS = 100000
MAX_REPORT_BYTES = 512000
MAX_STATE_RETRIES = 8
REQUIRED_HEADINGS = (
    "# QA Impact Analysis",
    "## Summary",
    "## Changed Areas",
    "## Risk Analysis",
    "## Recommended Checks",
    "## Regression Focus",
)


class CompletionCode(StrEnum):
    COMPLETED = "COMPLETED"
    RECONCILED = "RECONCILED"
    NOTHING_TO_RECONCILE = "NOTHING_TO_RECONCILE"
    DEPLOYMENT_NOT_FOUND = "DEPLOYMENT_NOT_FOUND"
    DEPLOYMENT_NOT_CLAIMED = "DEPLOYMENT_NOT_CLAIMED"
    CLAIM_TOKEN_INVALID = "CLAIM_TOKEN_INVALID"
    CLAIM_EXPIRED = "CLAIM_EXPIRED"
    REPORT_EMPTY = "REPORT_EMPTY"
    REPORT_TOO_LARGE = "REPORT_TOO_LARGE"
    REPORT_INVALID_STRUCTURE = "REPORT_INVALID_STRUCTURE"
    INVALID_ANALYSIS_OUTCOME = "INVALID_ANALYSIS_OUTCOME"
    COMPLETION_PAYLOAD_MISMATCH = "COMPLETION_PAYLOAD_MISMATCH"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    STORAGE_ERROR = "STORAGE_ERROR"
    AMBIGUOUS_DEPLOYMENT_ID = "AMBIGUOUS_DEPLOYMENT_ID"


@dataclass(frozen=True)
class CompletionResult:
    code: CompletionCode
    deployment_id: int
    succeeded: bool = False
    project_id: int | None = None
    report_path: str | None = None
    report_sha256: str | None = None
    report_chars: int | None = None
    report_bytes: int | None = None
    analysis_outcome: str | None = None
    completed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "deployment_id": self.deployment_id,
            "project_id": self.project_id,
            "succeeded": self.succeeded,
            "report_path": self.report_path,
            "report_sha256": self.report_sha256,
            "report_chars": self.report_chars,
            "report_bytes": self.report_bytes,
            "analysis_outcome": self.analysis_outcome,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class CompletionService:
    def __init__(
        self,
        store: EventStore,
        reports_root: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        crash_injector: Callable[[str], None] | None = None,
        reconcile_on_startup: bool = True,
    ) -> None:
        self._store = store
        self._reports_root = Path(reports_root)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._crash_injector = crash_injector
        if reconcile_on_startup:
            self.reconcile()

    def mark_analysis_completed(
        self,
        deployment_id: int,
        claim_token: str,
        report_markdown: str,
        analysis_outcome: str = "complete",
        project_id: int | None = None,
    ) -> CompletionResult:
        validated = _validate_payload(deployment_id, report_markdown, analysis_outcome)
        if isinstance(validated, CompletionResult):
            return validated
        report_bytes, digest = validated
        try:
            token_hash = _token_hash(claim_token)
        except (AttributeError, UnicodeEncodeError):
            return CompletionResult(CompletionCode.CLAIM_TOKEN_INVALID, deployment_id)

        intent_result = self._ensure_intent(
            deployment_id,
            token_hash,
            digest,
            len(report_markdown),
            len(report_bytes),
            analysis_outcome,
            project_id,
        )
        if isinstance(intent_result, CompletionResult):
            return intent_result
        intent, resolved_project_id = intent_result
        self._inject("after_intent")

        try:
            artifact = self._read_artifact(intent.report_path)
            if artifact is None:
                self._write_artifact(intent.report_path, report_bytes)
            elif not _artifact_matches(artifact, intent):
                return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
        except OSError:
            return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
        self._inject("after_artifact")
        return self._finalize(
            deployment_id,
            intent,
            CompletionCode.COMPLETED,
            project_id=resolved_project_id,
        )

    def reconcile(self) -> CompletionResult:
        found = False
        last_id = 0
        for _ in range(MAX_STATE_RETRIES):
            try:
                state = self._store.load()
            except StateStoreError:
                return CompletionResult(CompletionCode.STORAGE_ERROR, last_id)
            intents = [
                event
                for event in state.deployments.values()
                if event.status is EventStatus.CLAIMED and event.completion_intent is not None
            ]
            if not intents:
                return CompletionResult(
                    CompletionCode.RECONCILED if found else CompletionCode.NOTHING_TO_RECONCILE,
                    last_id,
                    succeeded=found,
                )
            made_progress = False
            for event in intents:
                assert event.completion_intent is not None
                last_id = event.deployment_id
                try:
                    artifact = self._read_artifact(event.completion_intent.report_path)
                except OSError:
                    return CompletionResult(CompletionCode.STORAGE_ERROR, event.deployment_id)
                if artifact is None:
                    continue
                if not _artifact_matches(artifact, event.completion_intent):
                    return CompletionResult(CompletionCode.STORAGE_ERROR, event.deployment_id)
                result = self._finalize(
                    event.deployment_id,
                    event.completion_intent,
                    CompletionCode.RECONCILED,
                    project_id=event.project_id,
                )
                if not result.succeeded:
                    return result
                found = made_progress = True
            if not made_progress:
                return CompletionResult(
                    CompletionCode.NOTHING_TO_RECONCILE, last_id, succeeded=False
                )
        return CompletionResult(CompletionCode.STORAGE_ERROR, last_id)

    def _ensure_intent(
        self,
        deployment_id: int,
        token_hash: str,
        digest: str,
        chars: int,
        byte_count: int,
        outcome: str,
        project_id: int | None,
    ) -> tuple[CompletionIntent, int] | CompletionResult:
        for _ in range(MAX_STATE_RETRIES):
            try:
                state = self._store.load()
            except StateStoreError:
                return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
            resolution = resolve_deployment(state, deployment_id, project_id)
            if resolution.error is not None:
                code = (
                    CompletionCode.AMBIGUOUS_DEPLOYMENT_ID
                    if resolution.error is ResolutionCode.AMBIGUOUS_DEPLOYMENT_ID
                    else CompletionCode.DEPLOYMENT_NOT_FOUND
                )
                return CompletionResult(code, deployment_id, project_id=project_id)
            event = resolution.event
            conflict = _completion_conflict(
                event, deployment_id, token_hash, digest, outcome, self._clock()
            )
            if conflict is not None:
                return conflict
            assert event is not None and event.claim is not None
            if event.completion_intent is not None:
                return event.completion_intent, event.project_id
            intent = CompletionIntent(
                token_hash=token_hash,
                report_sha256=digest,
                report_chars=chars,
                report_bytes=byte_count,
                report_path=_report_path(event),
                analysis_outcome=outcome,  # type: ignore[arg-type]
                started_at=self._clock(),
            )
            deployments = dict(state.deployments)
            assert resolution.key is not None
            deployments[resolution.key] = event.model_copy(
                update={"completion_intent": intent}
            )
            try:
                self._store.save(
                    state.model_copy(update={"deployments": deployments}),
                    expected_revision=state.revision,
                )
            except ConcurrentStateUpdateError:
                continue
            except StateStoreError:
                return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
            return intent, event.project_id
        return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)

    def _finalize(
        self,
        deployment_id: int,
        expected: CompletionIntent,
        success_code: CompletionCode,
        *,
        project_id: int | None = None,
    ) -> CompletionResult:
        for _ in range(MAX_STATE_RETRIES):
            try:
                state = self._store.load()
            except StateStoreError:
                return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
            resolution = resolve_deployment(state, deployment_id, project_id)
            event = resolution.event
            if event is None:
                code = (
                    CompletionCode.AMBIGUOUS_DEPLOYMENT_ID
                    if resolution.error is ResolutionCode.AMBIGUOUS_DEPLOYMENT_ID
                    else CompletionCode.DEPLOYMENT_NOT_FOUND
                )
                return CompletionResult(code, deployment_id, project_id=project_id)
            if event.status is EventStatus.COMPLETED:
                return _completed_result(event, expected, deployment_id)
            if event.completion_intent != expected:
                return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
            completed_at = self._clock()
            receipt = CompletionReceipt(
                **expected.model_dump(exclude={"started_at"}),
                completed_at=completed_at,
            )
            replacement = event.model_copy(
                update={
                    "status": EventStatus.COMPLETED,
                    "claim": None,
                    "completion_intent": None,
                    "completion_receipt": receipt,
                    "completed_at": completed_at,
                    "report_path": receipt.report_path,
                }
            )
            deployments = dict(state.deployments)
            assert resolution.key is not None
            deployments[resolution.key] = replacement
            environments = dict(state.environments)
            partition = environment_partition_key(event.project_id, event.environment)
            current_cursor = environments.get(partition, EnvironmentState())
            current_event = (
                state.deployments.get(
                    deployment_key(
                        event.project_id, current_cursor.last_completed_deployment_id
                    )
                )
                if current_cursor.last_completed_deployment_id is not None
                else None
            )
            if current_event is None or (event.finished_at, event.deployment_id) > (
                current_event.finished_at,
                current_event.deployment_id,
            ):
                environments[partition] = current_cursor.model_copy(
                    update={
                        "last_completed_deployment_id": event.deployment_id,
                        "last_completed_sha": event.sha,
                        "updated_at": completed_at,
                    }
                )
            try:
                self._store.save(
                    state.model_copy(
                        update={"deployments": deployments, "environments": environments}
                    ),
                    expected_revision=state.revision,
                )
            except ConcurrentStateUpdateError:
                continue
            except StateStoreError:
                return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)
            return _result_from_receipt(
                success_code, deployment_id, receipt, project_id=event.project_id
            )
        return CompletionResult(CompletionCode.STORAGE_ERROR, deployment_id)

    def _read_artifact(self, relative: str) -> bytes | None:
        path = self._safe_artifact_path(relative)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise OSError("unsafe report artifact")
        return path.read_bytes()

    def _write_artifact(self, relative: str, content: bytes) -> None:
        path = self._safe_artifact_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.parent.resolve() != path.parent:
            raise OSError("unsafe report directory")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink():
                raise OSError("unsafe report target")
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _safe_artifact_path(self, relative: str) -> Path:
        root = self._reports_root.absolute()
        if root.exists() and root.is_symlink():
            raise OSError("reports root cannot be a symlink")
        candidate = root.joinpath(*relative.split("/"))
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise OSError("report path escapes reports root") from exc
        for parent in candidate.parents:
            if parent == root:
                break
            if parent.exists() and parent.is_symlink():
                raise OSError("report path contains a symlink")
        return candidate

    def _inject(self, point: str) -> None:
        if self._crash_injector is not None:
            self._crash_injector(point)


def _validate_payload(
    deployment_id: int, report: object, outcome: object
) -> tuple[bytes, str] | CompletionResult:
    if not isinstance(report, str) or not report.strip():
        return CompletionResult(CompletionCode.REPORT_EMPTY, deployment_id)
    try:
        encoded = report.encode("utf-8")
    except UnicodeEncodeError:
        return CompletionResult(CompletionCode.REPORT_INVALID_STRUCTURE, deployment_id)
    if len(report) > MAX_REPORT_CHARS or len(encoded) > MAX_REPORT_BYTES:
        return CompletionResult(CompletionCode.REPORT_TOO_LARGE, deployment_id)
    if outcome not in {"complete", "partial"}:
        return CompletionResult(CompletionCode.INVALID_ANALYSIS_OUTCOME, deployment_id)
    headings = [line for line in report.splitlines() if line in REQUIRED_HEADINGS]
    if headings != list(REQUIRED_HEADINGS):
        return CompletionResult(CompletionCode.REPORT_INVALID_STRUCTURE, deployment_id)
    return encoded, hashlib.sha256(encoded).hexdigest()


def _completion_conflict(
    event: DeploymentEvent | None,
    deployment_id: int,
    token_hash: str,
    digest: str,
    outcome: str,
    now: datetime,
) -> CompletionResult | None:
    if event is None:
        return CompletionResult(CompletionCode.DEPLOYMENT_NOT_FOUND, deployment_id)
    if event.status is EventStatus.COMPLETED:
        if event.completion_receipt is None:
            return CompletionResult(CompletionCode.ALREADY_COMPLETED, deployment_id)
        receipt = event.completion_receipt
        if not hmac.compare_digest(token_hash, receipt.token_hash):
            return CompletionResult(CompletionCode.ALREADY_COMPLETED, deployment_id)
        if receipt.report_sha256 != digest or receipt.analysis_outcome != outcome:
            return CompletionResult(CompletionCode.COMPLETION_PAYLOAD_MISMATCH, deployment_id)
        return _result_from_receipt(
            CompletionCode.COMPLETED,
            deployment_id,
            receipt,
            project_id=event.project_id,
        )
    if event.status is not EventStatus.CLAIMED or event.claim is None:
        return CompletionResult(CompletionCode.DEPLOYMENT_NOT_CLAIMED, deployment_id)
    if not hmac.compare_digest(token_hash, event.claim.token_hash):
        return CompletionResult(CompletionCode.CLAIM_TOKEN_INVALID, deployment_id)
    if event.completion_intent is not None:
        intent = event.completion_intent
        if intent.report_sha256 != digest or intent.analysis_outcome != outcome:
            return CompletionResult(CompletionCode.COMPLETION_PAYLOAD_MISMATCH, deployment_id)
        return None
    if event.claim.expires_at <= now:
        return CompletionResult(CompletionCode.CLAIM_EXPIRED, deployment_id)
    return None


def _completed_result(
    event: DeploymentEvent, expected: CompletionIntent, deployment_id: int
) -> CompletionResult:
    receipt = event.completion_receipt
    if receipt is None or receipt.token_hash != expected.token_hash:
        return CompletionResult(CompletionCode.ALREADY_COMPLETED, deployment_id)
    if (
        receipt.report_sha256 != expected.report_sha256
        or receipt.analysis_outcome != expected.analysis_outcome
    ):
        return CompletionResult(CompletionCode.COMPLETION_PAYLOAD_MISMATCH, deployment_id)
    return _result_from_receipt(
        CompletionCode.COMPLETED,
        deployment_id,
        receipt,
        project_id=event.project_id,
    )


def _result_from_receipt(
    code: CompletionCode,
    deployment_id: int,
    receipt: CompletionReceipt,
    *,
    project_id: int | None = None,
) -> CompletionResult:
    return CompletionResult(
        code,
        deployment_id,
        succeeded=True,
        project_id=project_id,
        report_path=receipt.report_path,
        report_sha256=receipt.report_sha256,
        report_chars=receipt.report_chars,
        report_bytes=receipt.report_bytes,
        analysis_outcome=receipt.analysis_outcome,
        completed_at=receipt.completed_at,
    )


def _report_path(event: DeploymentEvent) -> str:
    return (
        f"projects/{event.project_id}/{event.environment}/"
        f"{event.deployment_id}-{event.sha}.md"
    )


def _token_hash(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def _artifact_matches(content: bytes, expected: CompletionIntent) -> bool:
    try:
        chars = len(content.decode("utf-8"))
    except UnicodeDecodeError:
        return False
    return (
        len(content) == expected.report_bytes
        and chars == expected.report_chars
        and hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected.report_sha256)
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
