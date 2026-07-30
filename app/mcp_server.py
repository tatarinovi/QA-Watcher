from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.claim_service import DEFAULT_CLAIM_TTL, ClaimCode, ClaimService
from app.completion_service import CompletionService
from app.context_service import ContextService
from app.pending_query import PendingQuery
from app.report_service import ReportService
from app.storage import EventStore, StateStoreError

TOOL_NAMES = (
    "list_pending_deployments",
    "claim_deployment",
    "get_deployment_context",
    "get_changed_files",
    "get_release_metadata",
    "mark_analysis_completed",
    "get_report",
)

LOOPBACK_HOSTS = [
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "[::1]",
    "[::1]:*",
]
LOOPBACK_ORIGINS = [
    f"{scheme}://{host}"
    for scheme in ("http", "https")
    for host in LOOPBACK_HOSTS
]


def create_mcp_server(
    store: EventStore,
    reports_root: str,
    *,
    clock: Any | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> FastMCP[None]:
    pending = PendingQuery(store, clock=clock)
    claims = ClaimService(store, clock=clock)
    context = ContextService(store, clock=clock)
    completion = CompletionService(store, reports_root, clock=clock)
    reports = ReportService(store, reports_root)
    server: FastMCP[None] = FastMCP(
        "qa-release",
        host="127.0.0.1",
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts or LOOPBACK_HOSTS,
            allowed_origins=allowed_origins or LOOPBACK_ORIGINS,
        ),
    )

    @server.tool()
    def list_pending_deployments(
        environment: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List unfinished deployments oldest-first."""
        try:
            page = pending.list_pending(
                environment=environment, cursor=cursor, limit=limit
            )
        except ValueError:
            return _error("INVALID_CURSOR")
        except StateStoreError:
            return _error("STORAGE_ERROR")
        items = [
            {
                "deployment_id": item.deployment_id,
                "project_id": item.project_id,
                "environment": item.environment,
                "sha": item.sha,
                "previous_sha": item.previous_sha,
                "deployed_at": item.finished_at,
                "claimable": item.claimable,
                "blocked_by_deployment_id": item.blocked_by_deployment_id,
                "blocked_by_project_id": item.blocked_by_project_id,
                "status": item.status,
                "changed_file_count": _changed_count(
                    store, item.project_id, item.deployment_id
                ),
            }
            for item in page.items
        ]
        return _success(
            "PENDING_DEPLOYMENTS_LISTED",
            {
                "items": items,
                "next_cursor": page.next_cursor,
                "has_more": page.next_cursor is not None,
            },
        )

    @server.tool()
    def claim_deployment(
        deployment_id: int,
        claim_token: str | None = None,
        lease_seconds: int = DEFAULT_CLAIM_TTL,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Acquire or renew the lease for the oldest unfinished deployment."""
        try:
            result = claims.claim_deployment(
                deployment_id,
                claim_token=claim_token,
                ttl_seconds=lease_seconds,
                project_id=project_id,
            )
        except StateStoreError:
            return _error("STORAGE_ERROR", deployment_id)
        code = {
            ClaimCode.DEPLOYMENT_COMPLETED: "DEPLOYMENT_NOT_PENDING",
            ClaimCode.INVALID_CLAIM_TTL: "INVALID_LEASE_SECONDS",
        }.get(result.code, result.code.value)
        if not result.succeeded:
            details = (
                {"blocked_by_deployment_id": result.blocked_by_deployment_id}
                if result.blocked_by_deployment_id is not None
                else None
            )
            if result.blocked_by_project_id is not None:
                details = details or {}
                details["blocked_by_project_id"] = result.blocked_by_project_id
            return _error(code, deployment_id, details, project_id)
        assert result.claim_token is not None and result.expires_at is not None
        return _success(
            result.code.value,
            {
                "deployment_id": deployment_id,
                "project_id": result.project_id,
                "claim_token": result.claim_token,
                "expires_at": _timestamp(result.expires_at),
            },
        )

    @server.tool()
    def get_deployment_context(
        deployment_id: int, claim_token: str, project_id: int | None = None
    ) -> dict[str, Any]:
        """Return compact immutable release context to the active claimant."""
        return context.get_deployment_context(deployment_id, claim_token, project_id)

    @server.tool()
    def get_changed_files(
        deployment_id: int,
        claim_token: str,
        cursor: str | None = None,
        limit: int = 100,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one bounded page of changed paths and factual flags."""
        return context.get_changed_files(
            deployment_id,
            claim_token,
            cursor=cursor,
            limit=limit,
            project_id=project_id,
        )

    @server.tool()
    def get_release_metadata(
        deployment_id: int, claim_token: str, project_id: int | None = None
    ) -> dict[str, Any]:
        """Return factual GitLab release metadata to the active claimant."""
        return context.get_release_metadata(deployment_id, claim_token, project_id)

    @server.tool()
    def mark_analysis_completed(
        deployment_id: int,
        claim_token: str,
        report_markdown: str,
        analysis_outcome: str = "complete",
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist a validated report and atomically complete the deployment."""
        result = completion.mark_analysis_completed(
            deployment_id,
            claim_token,
            report_markdown,
            analysis_outcome,
            project_id,
        )
        if not result.succeeded:
            return _error(result.code.value, deployment_id, project_id=project_id)
        return _success(
            result.code.value,
            {
                "deployment_id": deployment_id,
                "project_id": result.project_id,
                "report_sha256": result.report_sha256,
                "report_chars": result.report_chars,
                "report_bytes": result.report_bytes,
                "analysis_outcome": result.analysis_outcome,
                "completed_at": (
                    _timestamp(result.completed_at) if result.completed_at else None
                ),
            },
        )

    @server.tool()
    def get_report(
        deployment_id: int,
        cursor: str | None = None,
        max_chars: int = 20000,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one bounded chunk of a confirmed completed report."""
        result = reports.get_report(
            deployment_id,
            cursor=cursor,
            max_chars=max_chars,
            project_id=project_id,
        )
        if not result.succeeded:
            code = (
                "INVALID_CURSOR"
                if result.code.value == "INVALID_MAX_CHARS"
                else result.code.value
            )
            return _error(code, deployment_id, project_id=project_id)
        return _success(
            result.code.value,
            {
                "deployment_id": deployment_id,
                "project_id": result.project_id,
                "content": result.content,
                "next_cursor": result.next_cursor,
                "has_more": result.has_more,
                "total_chars": result.total_chars,
                "total_bytes": result.total_bytes,
                "sha256": result.sha256,
                "analysis_outcome": result.analysis_outcome,
            },
        )

    return server


def create_app(
    store: EventStore,
    reports_root: str,
    *,
    clock: Any | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> Any:
    """Build the safe-by-default Streamable HTTP ASGI application."""
    return create_mcp_server(
        store,
        reports_root,
        clock=clock,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    ).streamable_http_app()


def _success(code: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "ok": True, "code": code, "result": result}


def _error(
    code: str,
    deployment_id: int | None = None,
    details: dict[str, Any] | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code}
    if deployment_id is not None:
        error["deployment_id"] = deployment_id
    if project_id is not None:
        error["project_id"] = project_id
    if details:
        error["details"] = details
    return {"schema_version": 1, "ok": False, "error": error}


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _changed_count(store: EventStore, project_id: int, deployment_id: int) -> int | None:
    try:
        event = store.load().deployments.get(f"{project_id}:{deployment_id}")
    except StateStoreError:
        return None
    if event is None or event.compare_preview is None:
        return None
    return event.compare_preview.changed_file_count
