from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.deployment_identity import ResolutionCode, resolve_deployment
from app.models import EventStatus
from app.storage import EventStore, StateStoreError


class ReportCode(StrEnum):
    REPORT_AVAILABLE = "REPORT_AVAILABLE"
    DEPLOYMENT_NOT_FOUND = "DEPLOYMENT_NOT_FOUND"
    REPORT_NOT_AVAILABLE = "REPORT_NOT_AVAILABLE"
    INVALID_CURSOR = "INVALID_CURSOR"
    INVALID_MAX_CHARS = "INVALID_MAX_CHARS"
    STORAGE_ERROR = "STORAGE_ERROR"
    AMBIGUOUS_DEPLOYMENT_ID = "AMBIGUOUS_DEPLOYMENT_ID"


@dataclass(frozen=True)
class ReportResult:
    code: ReportCode
    deployment_id: int
    succeeded: bool = False
    project_id: int | None = None
    content: str | None = None
    next_cursor: str | None = None
    has_more: bool = False
    total_chars: int | None = None
    total_bytes: int | None = None
    sha256: str | None = None
    analysis_outcome: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "deployment_id": self.deployment_id,
            "project_id": self.project_id,
            "succeeded": self.succeeded,
            "content": self.content,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "total_chars": self.total_chars,
            "total_bytes": self.total_bytes,
            "sha256": self.sha256,
            "analysis_outcome": self.analysis_outcome,
        }


class ReportService:
    def __init__(self, store: EventStore, reports_root: Path | str) -> None:
        self._store = store
        self._reports_root = Path(reports_root)

    def get_report(
        self,
        deployment_id: int,
        *,
        cursor: str | None = None,
        max_chars: int = 20000,
        project_id: int | None = None,
    ) -> ReportResult:
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not 1 <= max_chars <= 20000
        ):
            return ReportResult(ReportCode.INVALID_MAX_CHARS, deployment_id)
        try:
            state = self._store.load()
        except StateStoreError:
            return ReportResult(ReportCode.STORAGE_ERROR, deployment_id)
        resolution = resolve_deployment(state, deployment_id, project_id)
        event = resolution.event
        if event is None:
            code = (
                ReportCode.AMBIGUOUS_DEPLOYMENT_ID
                if resolution.error is ResolutionCode.AMBIGUOUS_DEPLOYMENT_ID
                else ReportCode.DEPLOYMENT_NOT_FOUND
            )
            return ReportResult(code, deployment_id, project_id=project_id)
        if event.status is not EventStatus.COMPLETED or event.completion_receipt is None:
            return ReportResult(ReportCode.REPORT_NOT_AVAILABLE, deployment_id)
        receipt = event.completion_receipt
        offset = _decode_cursor(
            cursor, event.project_id, deployment_id, receipt.report_sha256
        )
        if offset is None:
            return ReportResult(ReportCode.INVALID_CURSOR, deployment_id)
        try:
            path = self._safe_path(receipt.report_path)
            if path.is_symlink() or not path.is_file():
                return ReportResult(ReportCode.STORAGE_ERROR, deployment_id)
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            return ReportResult(ReportCode.STORAGE_ERROR, deployment_id)
        if (
            len(raw) != receipt.report_bytes
            or len(content) != receipt.report_chars
            or not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(), receipt.report_sha256
            )
        ):
            return ReportResult(ReportCode.STORAGE_ERROR, deployment_id)
        if offset > len(content):
            return ReportResult(ReportCode.INVALID_CURSOR, deployment_id)
        end = min(offset + max_chars, len(content))
        has_more = end < len(content)
        return ReportResult(
            ReportCode.REPORT_AVAILABLE,
            deployment_id,
            succeeded=True,
            project_id=event.project_id,
            content=content[offset:end],
            next_cursor=(
                _encode_cursor(event.project_id, deployment_id, receipt.report_sha256, end)
                if has_more
                else None
            ),
            has_more=has_more,
            total_chars=receipt.report_chars,
            total_bytes=receipt.report_bytes,
            sha256=receipt.report_sha256,
            analysis_outcome=receipt.analysis_outcome,
        )

    def _safe_path(self, relative: str) -> Path:
        root = self._reports_root.absolute()
        if root.exists() and root.is_symlink():
            raise OSError("unsafe reports root")
        path = root.joinpath(*relative.split("/"))
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise OSError("report path escapes reports root") from exc
        for parent in path.parents:
            if parent == root:
                break
            if parent.exists() and parent.is_symlink():
                raise OSError("report path contains symlink")
        return path


def _encode_cursor(
    project_id: int, deployment_id: int, digest: str, offset: int
) -> str:
    payload = f"v2:{project_id}:{deployment_id}:{digest}:{offset}".encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None, project_id: int, deployment_id: int, digest: str
) -> int | None:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        version, raw_project, raw_id, raw_digest, raw_offset = decoded.split(":")
        offset = int(raw_offset)
    except (ValueError, UnicodeError):
        return None
    if (
        version != "v2"
        or raw_project != str(project_id)
        or raw_id != str(deployment_id)
        or not hmac.compare_digest(raw_digest, digest)
        or offset < 0
    ):
        return None
    return offset
