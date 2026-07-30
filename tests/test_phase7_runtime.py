from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.cli as watcher_cli
import app.main as runtime_main
from app.cli import _parser
from app.config import Settings
from app.deployment_ingestor import PollResult
from app.main import build_app, build_runtime
from app.scheduler import MultiProjectPoller


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "gitlab_url": "https://gitlab.example.com",
        "gitlab_project_id": 42,
        "gitlab_token": "test-only-token",
        "state_path": tmp_path / "data" / "state.json",
        "reports_path": tmp_path / "reports",
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_settings_fail_closed_for_non_loopback_and_validate_runtime_bounds(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        settings(tmp_path, bind_host="0.0.0.0")
    unsafe = settings(
        tmp_path,
        bind_host="0.0.0.0",
        allow_unsafe_non_loopback=True,
        mcp_allowed_hosts=["qa-watcher.internal", "qa-watcher.internal:*"],
    )
    assert unsafe.bind_host == "0.0.0.0"
    with pytest.raises(ValidationError):
        settings(tmp_path, poll_interval_seconds=4)
    with pytest.raises(ValidationError):
        settings(tmp_path, gitlab_url="file:///tmp/gitlab")
    with pytest.raises(ValidationError):
        settings(tmp_path, gitlab_token="")


def test_settings_parse_explicit_project_id_allowlist_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_PROJECT_IDS", "101, 102,201,101")
    monkeypatch.setenv("GITLAB_TOKEN", "test-only-token")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))

    config = Settings(_env_file=None)  # type: ignore[call-arg]

    assert config.gitlab_project_ids == (101, 102, 201)
    assert config.environment_branches == {
        "dev": "develop",
        "test": "testing",
        "stage": "stable",
        "prod": "master",
    }

    monkeypatch.setenv("GITLAB_PROJECT_IDS", "")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("GITLAB_PROJECT_IDS", "42,nope")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_telegram_settings_require_token_and_single_chat_id_together(tmp_path: Path) -> None:
    disabled = settings(tmp_path)
    assert disabled.telegram_bot_token is None
    assert disabled.telegram_chat_id is None
    assert disabled.telegram_message_thread_id is None

    with pytest.raises(ValidationError, match="Telegram"):
        settings(tmp_path, telegram_bot_token="123:test")
    with pytest.raises(ValidationError, match="Telegram"):
        settings(tmp_path, telegram_chat_id="-1001234567890")
    with pytest.raises(ValidationError, match="Telegram"):
        settings(tmp_path, telegram_message_thread_id=42)

    enabled = settings(
        tmp_path,
        telegram_bot_token="123:test",
        telegram_chat_id="-1001234567890",
        telegram_message_thread_id=42,
    )
    assert enabled.telegram_bot_token is not None
    assert enabled.telegram_bot_token.get_secret_value() == "123:test"
    assert enabled.telegram_chat_id == "-1001234567890"
    assert enabled.telegram_message_thread_id == 42


def test_runtime_builds_one_branch_guarded_poller_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[tuple[int, dict[str, str]]] = []

    class FakeClient:
        def __init__(
            self,
            _http: object,
            project_id: int,
            *,
            environment_branches: dict[str, str],
        ) -> None:
            created.append((project_id, environment_branches))

    class FakeIngestor:
        def __init__(self, _client: object, _store: object) -> None:
            pass

        def poll(self) -> object:
            raise AssertionError("not called during runtime construction")

    monkeypatch.setattr(runtime_main, "GitLabClient", FakeClient)
    monkeypatch.setattr(runtime_main, "DeploymentIngestor", FakeIngestor)
    config = settings(
        tmp_path,
        gitlab_project_id=None,
        gitlab_project_ids=(42, 43),
    )

    runtime = build_runtime(config)
    runtime.http.close()

    assert created == [
        (42, config.environment_branches),
        (43, config.environment_branches),
    ]
    assert runtime.poller.pollers_count == 2


def test_runtime_wraps_project_pollers_with_env_configured_telegram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifier_args: list[tuple[str, str, int | None, float]] = []
    wrappers: list[tuple[object, object]] = []

    class FakeTelegramNotifier:
        def __init__(
            self,
            *,
            token: str,
            chat_id: str,
            message_thread_id: int | None,
            timeout_seconds: float,
        ) -> None:
            notifier_args.append((token, chat_id, message_thread_id, timeout_seconds))

    class FakeNotifyingPoller:
        def __init__(self, poller: object, notifier: object) -> None:
            wrappers.append((poller, notifier))

        def poll(self) -> PollResult:
            return PollResult(added=0, rejected=0)

    monkeypatch.setattr(runtime_main, "TelegramNotifier", FakeTelegramNotifier)
    monkeypatch.setattr(runtime_main, "NotifyingPoller", FakeNotifyingPoller)
    config = settings(
        tmp_path,
        telegram_bot_token="123:test",
        telegram_chat_id="-1001234567890",
        telegram_message_thread_id=42,
        telegram_timeout_seconds=7,
    )

    runtime = build_runtime(config)
    runtime.http.close()

    assert notifier_args == [("123:test", "-1001234567890", 42, 7)]
    assert len(wrappers) == 1


def test_multi_project_poller_isolates_a_failed_project() -> None:
    class GoodPoller:
        def poll(self) -> PollResult:
            return PollResult(added=2, rejected=1, rejection_reasons=("bad_ref",))

    class FailedPoller:
        def poll(self) -> PollResult:
            raise RuntimeError("sensitive backend detail")

    result = MultiProjectPoller([FailedPoller(), GoodPoller()]).poll()

    assert result.added == 2
    assert result.rejected == 2
    assert result.rejection_reasons == ("project_poll_failed", "bad_ref")


def test_health_and_gitlab_neutral_readiness_create_private_storage(tmp_path: Path) -> None:
    config = settings(tmp_path)
    app = build_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        health = client.get("/health")
        ready = client.get("/ready")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert config.state_path.exists()
    assert config.reports_path.is_dir()
    assert config.state_path.stat().st_mode & 0o077 == 0


def test_cli_show_state_and_list_pending_are_json_and_secret_free(tmp_path: Path) -> None:
    config = settings(tmp_path)
    assert not config.state_path.exists()
    env = {
        **os.environ,
        "GITLAB_URL": str(config.gitlab_url),
        "GITLAB_PROJECT_IDS": "42",
        "GITLAB_TOKEN": "never-print-this-token",
        "STATE_PATH": str(config.state_path),
        "REPORTS_PATH": str(config.reports_path),
        "SCHEDULER_ENABLED": "false",
    }
    env.pop("GITLAB_PROJECT_ID", None)
    for command in ("show-state", "list-pending"):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", command],
            cwd=Path(__file__).parents[1],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        json.loads(result.stdout)
        assert "never-print-this-token" not in result.stdout + result.stderr


def test_cli_accepts_isolated_real_gitlab_replay_command() -> None:
    args = _parser().parse_args(
        [
            "replay",
            "--project-id",
            "123",
            "--environment",
            "test",
            "--count",
            "3",
            "--port",
            "18788",
        ]
    )

    assert args.command == "replay"
    assert args.project_id == 123
    assert args.environment == "test"
    assert args.count == 3
    assert args.port == 18788


def test_cli_dispatches_replay_without_building_production_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_PROJECT_IDS", "123")
    monkeypatch.setenv("GITLAB_TOKEN", "test-only-token")
    called: dict[str, object] = {}

    def fake_run_replay(
        settings: Settings, *, project_id: int, environment: str, count: int, port: int
    ) -> int:
        called.update(
            settings=settings,
            project_id=project_id,
            environment=environment,
            count=count,
            port=port,
        )
        return 0

    monkeypatch.setattr(watcher_cli, "run_replay", fake_run_replay)

    result = watcher_cli.main(
        [
            "replay",
            "--project-id",
            "123",
            "--environment",
            "test",
            "--count",
            "3",
            "--port",
            "18788",
        ]
    )

    assert result == 0
    assert called["project_id"] == 123
    assert called["environment"] == "test"
    assert called["count"] == 3
    assert called["port"] == 18788


def test_run_replay_removes_disposable_storage_after_server_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay_root = tmp_path / "replay"

    def make_replay_dir(*_args: object, **_kwargs: object) -> str:
        replay_root.mkdir()
        return str(replay_root)

    class FakeHttp:
        def close(self) -> None:
            pass

    monkeypatch.setattr(watcher_cli.tempfile, "mkdtemp", make_replay_dir)
    monkeypatch.setattr(watcher_cli.httpx, "Client", lambda **_kwargs: FakeHttp())
    monkeypatch.setattr(watcher_cli, "GitLabClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        watcher_cli,
        "prepare_replay",
        lambda *_args, **_kwargs: SimpleNamespace(
            project_id=42,
            environment="test",
            deployment_ids=(1, 2, 3),
            pending_count=2,
        ),
    )
    monkeypatch.setattr(watcher_cli, "create_mcp_app", lambda *_args: object())
    monkeypatch.setattr(watcher_cli.uvicorn, "run", lambda *_args, **_kwargs: None)

    result = watcher_cli.run_replay(
        settings(tmp_path),
        project_id=42,
        environment="test",
        count=3,
        port=18788,
    )

    assert result == 0
    assert not replay_root.exists()


def test_readiness_fails_with_503_for_corrupt_state(tmp_path: Path) -> None:
    config = settings(tmp_path)
    app = build_app(config)
    config.state_path.write_text("not-json", encoding="utf-8")
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_readiness_fails_when_state_directory_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings(tmp_path)
    app = build_app(config)
    original_probe = runtime_main._write_probe

    def fail_state_probe(directory: Path) -> None:
        if directory == config.state_path.parent:
            raise OSError("read-only state directory")
        original_probe(directory)

    monkeypatch.setattr(runtime_main, "_write_probe", fail_state_probe)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/ready")
    assert response.status_code == 503


def test_container_contract_is_loopback_only_and_has_no_ai_runtime() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    combined = f"{compose}\n{dockerfile}\n{env_example}".lower()

    assert '"127.0.0.1:${watcher_port:-8787}:8000"' in compose.lower()
    assert "watcher-data:/app/data" in compose
    assert "watcher-reports:/app/reports" in compose
    assert "./data:/app/data" not in compose
    assert "./reports:/app/reports" not in compose
    assert "app.cli" in dockerfile
    assert "gitlab_token=" in env_example.lower()
    for forbidden in ("ai_api", "openai", "anthropic", "ollama", "prompts/", "git clone"):
        assert forbidden not in combined
    for legacy in (
        "app/ai_client.py",
        "app/context_builder.py",
        "app/deployment_checker.py",
        "app/git_service.py",
        "prompts/qa_analysis.md",
    ):
        assert not (root / legacy).exists()
