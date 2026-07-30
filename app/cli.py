from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from sys import stderr
from typing import Any

import httpx
import uvicorn

from app.config import Settings
from app.context_service import ContextService
from app.gitlab_client import GitLabClient, GitLabProtocolError
from app.main import build_app, build_runtime
from app.mcp_server import create_app as create_mcp_app
from app.pending_query import PendingQuery
from app.replay import ReplayError, prepare_replay
from app.storage import JsonEventStore


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = Settings()  # type: ignore[call-arg]
    if args.command == "replay":
        return run_replay(
            settings,
            project_id=args.project_id,
            environment=args.environment,
            count=args.count,
            port=args.port,
        )
    if args.command == "serve":
        uvicorn.run(
            build_app(settings),
            host=settings.bind_host,
            port=settings.port,
            log_level=settings.log_level.lower(),
        )
        return 0
    runtime = build_runtime(settings)
    try:
        if args.command == "poll":
            result = runtime.poller.poll()
            _print_json(
                {
                    "added": result.added,
                    "rejected": result.rejected,
                    "rejection_reasons": result.rejection_reasons,
                }
            )
            return 0
        if args.command == "list-pending":
            page = PendingQuery(runtime.store).list_pending(
                environment=args.environment,
                cursor=args.cursor,
                limit=args.limit,
            )
            _print_json(
                {
                    "items": [item.__dict__ for item in page.items],
                    "next_cursor": page.next_cursor,
                }
            )
            return 0
        if args.command == "show-context":
            _print_json(
                ContextService(runtime.store).get_deployment_context(
                    args.deployment_id, args.claim_token
                )
            )
            return 0
        if args.command == "show-state":
            _print_json(runtime.store.load().model_dump(mode="json"))
            return 0
    finally:
        runtime.http.close()
    parser.error("unknown command")
    return 2


def run_replay(
    settings: Settings,
    *,
    project_id: int,
    environment: str,
    count: int,
    port: int,
) -> int:
    """Prepare and serve a read-only, disposable replay from real GitLab facts."""

    if project_id not in settings.gitlab_project_ids:
        print("replay project must be present in GITLAB_PROJECT_IDS", file=stderr)
        return 2
    work_dir = Path(tempfile.mkdtemp(prefix="qa-watcher-replay-"))
    data_dir = work_dir / "data"
    reports_dir = work_dir / "reports"
    data_dir.mkdir(mode=0o700)
    reports_dir.mkdir(mode=0o700)
    state_path = data_dir / "state.json"
    store = JsonEventStore(state_path)
    store.save(store.load(), expected_revision=0)
    if os.name != "nt":
        state_path.chmod(0o600)

    http: httpx.Client | None = None
    try:
        http = httpx.Client(
            base_url=f"{str(settings.gitlab_url).rstrip('/')}/api/v4",
            headers={"PRIVATE-TOKEN": settings.gitlab_token.get_secret_value()},
            timeout=settings.gitlab_timeout_seconds,
            follow_redirects=False,
        )
        client = GitLabClient(
            http,
            project_id,
            environment_branches=settings.environment_branches,
        )
        summary = prepare_replay(
            client, store, environment=environment, count=count
        )
        _print_json(
            {
                "mode": "isolated_real_gitlab_replay",
                "project_id": summary.project_id,
                "environment": summary.environment,
                "deployment_ids": summary.deployment_ids,
                "pending_count": summary.pending_count,
                "state_path": str(state_path),
                "reports_path": str(reports_dir),
                "mcp_url": f"http://127.0.0.1:{port}/mcp",
            }
        )
        uvicorn.run(
            create_mcp_app(store, str(reports_dir)),
            host="127.0.0.1",
            port=port,
            log_level=settings.log_level.lower(),
        )
    except (GitLabProtocolError, ReplayError, OSError) as exc:
        print(f"replay failed: {exc}", file=stderr)
        return 2
    finally:
        if http is not None:
            http.close()
        shutil.rmtree(work_dir, ignore_errors=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-watcher")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run polling and MCP HTTP server")
    commands.add_parser("poll", help="run one GitLab polling cycle")
    pending = commands.add_parser("list-pending", help="list pending events as JSON")
    pending.add_argument("--environment")
    pending.add_argument("--cursor")
    pending.add_argument("--limit", type=int, default=20)
    context = commands.add_parser("show-context", help="show claimed factual context")
    context.add_argument("deployment_id", type=int)
    context.add_argument("--claim-token", required=True)
    commands.add_parser("show-state", help="show durable state as JSON")
    replay = commands.add_parser(
        "replay", help="replay recent real GitLab deployments in isolated local storage"
    )
    replay.add_argument("--project-id", type=int, required=True)
    replay.add_argument("--environment", choices=("dev", "test", "stage", "prod"), required=True)
    replay.add_argument("--count", type=int, default=3, choices=range(2, 21))
    replay.add_argument("--port", type=int, default=18788, choices=range(1024, 65536))
    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
