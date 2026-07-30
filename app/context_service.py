from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.completion_service import MAX_REPORT_BYTES, MAX_REPORT_CHARS, REQUIRED_HEADINGS
from app.deployment_identity import ResolutionCode, resolve_deployment
from app.models import ChangedFilePreview, DeploymentEvent, EventStatus
from app.storage import EventStore, StateStoreError

MAX_CHANGED_FILES_LIMIT = 200


class ContextCode(StrEnum):
    CONTEXT_AVAILABLE = "CONTEXT_AVAILABLE"
    CHANGED_FILES_AVAILABLE = "CHANGED_FILES_AVAILABLE"
    RELEASE_METADATA_AVAILABLE = "RELEASE_METADATA_AVAILABLE"
    DEPLOYMENT_NOT_FOUND = "DEPLOYMENT_NOT_FOUND"
    DEPLOYMENT_NOT_PENDING = "DEPLOYMENT_NOT_PENDING"
    CLAIM_TOKEN_INVALID = "CLAIM_TOKEN_INVALID"
    CLAIM_EXPIRED = "CLAIM_EXPIRED"
    INVALID_CURSOR = "INVALID_CURSOR"
    COMPARE_UNAVAILABLE = "COMPARE_UNAVAILABLE"
    STORAGE_ERROR = "STORAGE_ERROR"
    AMBIGUOUS_DEPLOYMENT_ID = "AMBIGUOUS_DEPLOYMENT_ID"


class ContextService:
    """Read-only projections for the claim holder.

    The service deliberately returns portable factual metadata only. It never
    returns compare diff bodies, local paths, credentials, or the complete file
    list as part of the compact context.
    """

    def __init__(
        self,
        store: EventStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_deployment_context(
        self, deployment_id: int, claim_token: str, project_id: int | None = None
    ) -> dict[str, Any]:
        event, error = self._authenticated_event(deployment_id, claim_token, project_id)
        if error is not None:
            return error
        assert event is not None
        compare = event.compare_preview
        if compare is None:
            return _error(ContextCode.COMPARE_UNAVAILABLE, deployment_id)
        return _success(
            ContextCode.CONTEXT_AVAILABLE,
            {
                "deployment_id": event.deployment_id,
                "project_id": event.project_id,
                "environment": event.environment,
                "old_sha": event.previous_sha,
                "new_sha": event.sha,
                "deployed_at": _timestamp(event.finished_at),
                "pipeline": {"id": event.pipeline_id, "ref": event.pipeline_ref},
                "deployment": {
                    "id": event.deployment_id,
                    "finished_at": _timestamp(event.finished_at),
                },
                "project": _project(event),
                "commit": _commit(event),
                "compare": _compare_summary(event),
                "limitations": _limitations(event),
                "report_contract": {
                    "schema_version": 1,
                    "required_headings": list(REQUIRED_HEADINGS),
                    "analysis_outcomes": ["complete", "partial"],
                    "max_chars": MAX_REPORT_CHARS,
                    "max_bytes": MAX_REPORT_BYTES,
                },
            },
        )

    def get_changed_files(
        self,
        deployment_id: int,
        claim_token: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            return _error(ContextCode.INVALID_CURSOR, deployment_id)
        event, error = self._authenticated_event(deployment_id, claim_token, project_id)
        if error is not None:
            return error
        assert event is not None
        compare = event.compare_preview
        if compare is None:
            return _error(ContextCode.COMPARE_UNAVAILABLE, deployment_id)
        offset = _decode_cursor(cursor, event)
        if offset is None or offset > len(compare.changed_files):
            return _error(ContextCode.INVALID_CURSOR, deployment_id)
        end = min(offset + limit, len(compare.changed_files))
        has_more = end < len(compare.changed_files)
        return _success(
            ContextCode.CHANGED_FILES_AVAILABLE,
            {
                "deployment_id": deployment_id,
                "project_id": event.project_id,
                "items": [_changed_file(item) for item in compare.changed_files[offset:end]],
                "next_cursor": _encode_cursor(event, end) if has_more else None,
                "has_more": has_more,
                "summary": _compare_summary(event),
                "limitations": _limitations(event),
            },
        )

    def get_release_metadata(
        self, deployment_id: int, claim_token: str, project_id: int | None = None
    ) -> dict[str, Any]:
        event, error = self._authenticated_event(deployment_id, claim_token, project_id)
        if error is not None:
            return error
        assert event is not None
        return _success(
            ContextCode.RELEASE_METADATA_AVAILABLE,
            {
                "project_id": event.project_id,
                "deployment": {
                    "id": event.deployment_id,
                    "environment": event.environment,
                    "sha": event.sha,
                    "previous_sha": event.previous_sha,
                    "finished_at": _timestamp(event.finished_at),
                },
                "pipeline": {"id": event.pipeline_id, "ref": event.pipeline_ref},
                "project": _project(event),
                "commit": _commit(event),
            },
        )

    def _authenticated_event(
        self, deployment_id: int, claim_token: str, project_id: int | None = None
    ) -> tuple[DeploymentEvent | None, dict[str, Any] | None]:
        try:
            state = self._store.load()
        except StateStoreError:
            return None, _error(ContextCode.STORAGE_ERROR, deployment_id)
        resolution = resolve_deployment(state, deployment_id, project_id)
        event = resolution.event
        if event is None:
            code = (
                ContextCode.AMBIGUOUS_DEPLOYMENT_ID
                if resolution.error is ResolutionCode.AMBIGUOUS_DEPLOYMENT_ID
                else ContextCode.DEPLOYMENT_NOT_FOUND
            )
            return None, _error(code, deployment_id, project_id)
        if event.status is not EventStatus.CLAIMED or event.claim is None:
            return None, _error(ContextCode.DEPLOYMENT_NOT_PENDING, deployment_id)
        if event.claim.expires_at <= self._clock():
            return None, _error(ContextCode.CLAIM_EXPIRED, deployment_id)
        try:
            supplied_hash = f"sha256:{hashlib.sha256(claim_token.encode()).hexdigest()}"
        except (AttributeError, UnicodeEncodeError):
            return None, _error(ContextCode.CLAIM_TOKEN_INVALID, deployment_id)
        if not hmac.compare_digest(supplied_hash, event.claim.token_hash):
            return None, _error(ContextCode.CLAIM_TOKEN_INVALID, deployment_id)
        return event, None


def _success(code: ContextCode, result: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "ok": True, "code": code.value, "result": result}


def _error(
    code: ContextCode, deployment_id: int, project_id: int | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code.value, "deployment_id": deployment_id}
    if project_id is not None:
        error["project_id"] = project_id
    return {
        "schema_version": 1,
        "ok": False,
        "error": error,
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    if parsed.scheme == "ssh":
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(("ssh", host + port, parsed.path, "", ""))
    clean = value.split("?", 1)[0].split("#", 1)[0]
    if "@" in clean:
        _userinfo, separator, host_path = clean.partition("@")
        host, colon, path = host_path.partition(":")
        if separator and host and colon and path:
            return f"ssh://{host}/{path.lstrip('/')}"
    return clean


def _project(event: DeploymentEvent) -> dict[str, Any] | None:
    project = event.project
    if project is None:
        return None
    return {
        "gitlab_host": project.gitlab_host,
        "project_id": project.project_id,
        "name": project.name,
        "web_url": _safe_url(project.web_url),
        "repository_urls": {
            "http": _safe_url(project.http_repo_url),
            "ssh": _safe_url(project.ssh_repo_url),
        },
    }


def _commit(event: DeploymentEvent) -> dict[str, Any] | None:
    commit = event.commit
    if commit is None:
        return None
    return {
        "sha": commit.sha,
        "title": commit.title,
        "author_name": commit.author_name,
        "authored_date": commit.authored_date,
        "web_url": _safe_url(commit.web_url),
    }


def _compare_summary(event: DeploymentEvent) -> dict[str, Any]:
    compare = event.compare_preview
    assert compare is not None
    partial = compare.files_truncated or compare.incomplete or compare.compare_timeout
    return {
        "changed_file_count": compare.changed_file_count,
        "changed_file_count_semantics": "lower_bound" if partial else "exact",
        "added_files": compare.added_files,
        "renamed_files": compare.renamed_files,
        "deleted_files": compare.deleted_files,
        "generated_files": compare.generated_files,
        "same_ref": compare.same_ref,
        "compare_timeout": compare.compare_timeout,
        "compare_incomplete": compare.incomplete,
        "files_truncated": compare.files_truncated,
    }


def _limitations(event: DeploymentEvent) -> list[str]:
    compare = event.compare_preview
    assert compare is not None
    limitations = ["raw_diff_not_available"]
    if compare.files_truncated or compare.incomplete or compare.compare_timeout:
        limitations.extend(
            [
                "changed_file_count_is_a_lower_bound",
                "missing_path_does_not_prove_unchanged",
            ]
        )
    if compare.compare_timeout:
        limitations.append("compare_timed_out")
    if compare.incomplete:
        limitations.append("compare_incomplete")
    if compare.files_truncated:
        limitations.append("changed_files_truncated")
    return limitations


def _changed_file(item: ChangedFilePreview) -> dict[str, Any]:
    return {
        "old_path": item.old_path,
        "new_path": item.new_path,
        "new_file": item.added,
        "renamed_file": item.renamed,
        "deleted_file": item.deleted,
        "generated_file": item.generated,
        "collapsed": item.collapsed,
        "too_large": item.too_large,
    }


def _cursor_binding(event: DeploymentEvent) -> str:
    raw = (
        f"{event.project_id}:{event.deployment_id}:{event.previous_sha}:{event.sha}"
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _encode_cursor(event: DeploymentEvent, offset: int) -> str:
    raw = json.dumps(["v1", _cursor_binding(event), offset], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, event: DeploymentEvent) -> int | None:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if (
            not isinstance(value, list)
            or len(value) != 3
            or value[0] != "v1"
            or not isinstance(value[1], str)
            or isinstance(value[2], bool)
            or not isinstance(value[2], int)
            or value[2] < 0
            or not hmac.compare_digest(value[1], _cursor_binding(event))
        ):
            return None
        return value[2]
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
