from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, _is_loopback
from app.deployment_ingestor import DeploymentIngestor
from app.gitlab_client import GitLabClient
from app.mcp_server import LOOPBACK_HOSTS, LOOPBACK_ORIGINS
from app.mcp_server import create_app as create_mcp_app
from app.scheduler import MultiProjectPoller, NotifyingPoller, Poller, PollScheduler
from app.storage import JsonEventStore, StateStoreError
from app.telegram_notifier import TelegramNotifier


@dataclass
class Runtime:
    store: JsonEventStore
    http: httpx.Client
    poller: MultiProjectPoller
    scheduler: PollScheduler | None


def build_runtime(settings: Settings) -> Runtime:
    store = JsonEventStore(settings.state_path)
    http = httpx.Client(
        base_url=f"{str(settings.gitlab_url).rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": settings.gitlab_token.get_secret_value()},
        timeout=settings.gitlab_timeout_seconds,
        follow_redirects=False,
    )
    notifier = None
    if settings.telegram_bot_token is not None and settings.telegram_chat_id is not None:
        notifier = TelegramNotifier(
            token=settings.telegram_bot_token.get_secret_value(),
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
            timeout_seconds=settings.telegram_timeout_seconds,
        )
    project_pollers: list[Poller] = []
    for project_id in settings.gitlab_project_ids:
        project_poller: Poller = DeploymentIngestor(
            GitLabClient(
                http,
                project_id,
                environment_branches=settings.environment_branches,
            ),
            store,
        )
        if notifier is not None:
            project_poller = NotifyingPoller(project_poller, notifier)
        project_pollers.append(project_poller)
    poller = MultiProjectPoller(project_pollers)
    scheduler = (
        PollScheduler(poller, settings.poll_interval_seconds)
        if settings.scheduler_enabled
        else None
    )
    return Runtime(store=store, http=http, poller=poller, scheduler=scheduler)


def build_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()  # type: ignore[call-arg]
    runtime = build_runtime(config)
    _prepare_storage(config, runtime.store)
    allowed_hosts = list(LOOPBACK_HOSTS)
    allowed_origins = list(LOOPBACK_ORIGINS)
    if not _is_loopback(config.bind_host):
        allowed_hosts.extend(config.mcp_allowed_hosts)
        for host in config.mcp_allowed_hosts:
            allowed_origins.extend((f"http://{host}", f"https://{host}"))
    mcp_app = create_mcp_app(
        runtime.store,
        str(config.reports_path),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            if runtime.scheduler is not None:
                runtime.scheduler.start()
            try:
                yield
            finally:
                if runtime.scheduler is not None:
                    runtime.scheduler.shutdown()
                runtime.http.close()

    app = FastAPI(title="QA Watcher", lifespan=lifespan)
    app.state.settings = config
    app.state.runtime = runtime

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        try:
            runtime.store.load()
            _write_probe(config.state_path.parent)
            _write_probe(config.reports_path)
        except (OSError, StateStoreError):
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.mount("/", mcp_app)
    return app


def _prepare_storage(settings: Settings, store: JsonEventStore) -> None:
    settings.state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.reports_path.mkdir(parents=True, exist_ok=True)
    _restrict_directory(settings.state_path.parent)
    _restrict_directory(settings.reports_path)
    if not settings.state_path.exists():
        store.save(store.load(), expected_revision=0)
    if os.name != "nt":
        settings.state_path.chmod(0o600)
    _write_probe(settings.reports_path)


def _write_probe(directory: Path) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".ready-", dir=directory)
    path = Path(name)
    try:
        os.write(descriptor, b"ready")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)
