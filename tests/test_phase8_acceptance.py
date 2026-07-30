from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.completion_service import REQUIRED_HEADINGS
from app.deployment_ingestor import DeploymentIngestor
from app.gitlab_client import GitLabClient
from app.mcp_server import TOOL_NAMES, create_app
from app.storage import JsonEventStore


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _fixture_repository(path: Path) -> tuple[str, str, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", "qa@example.test")
    _git(path, "config", "user.name", "QA Fixture")
    (path / "release.txt").write_text("3.20\n", encoding="utf-8")
    _git(path, "add", "release.txt")
    _git(path, "commit", "-q", "-m", "TEST 3.20")
    sha_320 = _git(path, "rev-parse", "HEAD")
    (path / "release.txt").write_text("3.21\n", encoding="utf-8")
    _git(path, "commit", "-qam", "TEST 3.21")
    sha_321 = _git(path, "rev-parse", "HEAD")
    (path / "risk.txt").write_text("new risk\n", encoding="utf-8")
    _git(path, "add", "risk.txt")
    _git(path, "commit", "-q", "-m", "TEST 3.22")
    sha_322 = _git(path, "rev-parse", "HEAD")
    return sha_320, sha_321, sha_322


def _fake_gitlab(
    shas: tuple[str, str, str], visible: set[int]
) -> FastAPI:
    app = FastAPI()
    by_deployment = {320: shas[0], 321: shas[1], 322: shas[2]}

    @app.get("/api/v4/projects/42/deployments")
    def deployments(response: Response) -> list[dict[str, Any]]:
        response.headers["X-Next-Page"] = ""
        return [
            {
                "id": deployment_id,
                "status": "success",
                "sha": by_deployment[deployment_id],
                "finished_at": f"2026-07-29T18:{deployment_id - 320:02d}:00+00:00",
                "environment": {"name": "TEST"},
                "deployable": {
                    "status": "success",
                    "pipeline": {"id": 9000 + deployment_id},
                },
            }
            for deployment_id in sorted(visible)
        ]

    @app.get("/api/v4/projects/42/pipelines/{pipeline_id}")
    def pipeline(pipeline_id: int) -> dict[str, Any]:
        deployment_id = pipeline_id - 9000
        return {
            "id": pipeline_id,
            "status": "success",
            "sha": by_deployment[deployment_id],
            "ref": "testing",
        }

    @app.get("/api/v4/projects/42")
    def project(request: Request) -> dict[str, Any]:
        origin = str(request.base_url).rstrip("/")
        return {
            "id": 42,
            "name": "qa-fixture",
            "web_url": f"{origin}/group/qa-fixture",
            "http_url_to_repo": f"{origin}/group/qa-fixture.git",
            "ssh_url_to_repo": "git@127.0.0.1:group/qa-fixture.git",
        }

    @app.get("/api/v4/projects/42/repository/commits/{sha}")
    def commit(sha: str, request: Request) -> dict[str, Any]:
        assert sha in shas
        return {
            "id": sha,
            "title": f"TEST 3.{20 + shas.index(sha)}",
            "author_name": "QA Fixture",
            "authored_date": "2026-07-29T18:00:00+00:00",
            "web_url": f"{str(request.base_url).rstrip('/')}/group/qa-fixture/-/commit/{sha}",
        }

    @app.get("/api/v4/projects/42/repository/compare")
    def compare(from_: str | None = None, to: str | None = None) -> dict[str, Any]:
        # FastAPI maps this explicitly below because `from` is a Python keyword.
        del from_, to
        return {}

    # Replace the convenience route with one that reads the exact GitLab query names.
    app.router.routes.pop()

    @app.get("/api/v4/projects/42/repository/compare")
    def compare_exact(request: Request) -> dict[str, Any]:
        old_sha = request.query_params["from"]
        new_sha = request.query_params["to"]
        assert (old_sha, new_sha) in {(shas[0], shas[1]), (shas[1], shas[2])}
        path = "release.txt" if new_sha == shas[1] else "risk.txt"
        return {
            "compare_timeout": False,
            "compare_same_ref": False,
            "diffs": [
                {
                    "old_path": path,
                    "new_path": path,
                    "new_file": new_sha == shas[2],
                    "renamed_file": False,
                    "deleted_file": False,
                    "generated_file": False,
                    "collapsed": False,
                    "too_large": False,
                }
            ],
        }

    return app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _serve(app: Any) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise AssertionError("server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _payload(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    assert isinstance(payload, dict)
    return payload


def _report(deployment_id: int, old_sha: str, new_sha: str) -> str:
    sections = [
        REQUIRED_HEADINGS[0],
        REQUIRED_HEADINGS[1] + f"\nTEST deployment {deployment_id}: {old_sha}..{new_sha}",
        REQUIRED_HEADINGS[2] + "\nDirect local Git object diff verified.",
        REQUIRED_HEADINGS[3] + "\nRelease behavior changed.",
        REQUIRED_HEADINGS[4] + "\nRun focused release checks.",
        REQUIRED_HEADINGS[5] + "\nVerify adjacent TEST rollout behavior.",
    ]
    return "\n\n".join(sections) + "\n"


def test_skill_contract_is_exact_and_secret_safe() -> None:
    root = Path(__file__).parents[1]
    skill = (root / "skills/qa-release-analysis/SKILL.md").read_text(encoding="utf-8")
    mapping = (
        root / "skills/qa-release-analysis/templates/repositories.example.yaml"
    ).read_text(encoding="utf-8")
    assert skill.startswith("---\nname: qa-release-analysis\n")
    assert "hermes mcp add qa-release --url http://127.0.0.1:8787/mcp" in skill
    assert "hermes mcp test qa-release" in skill
    for tool_name in TOOL_NAMES:
        assert f"mcp_qa_release_{tool_name}" in skill
    for heading in REQUIRED_HEADINGS:
        assert heading in skill
    assert "old_sha..new_sha" in skill
    assert "Never write it to a file" in skill
    assert "schema_version: 1" in mapping
    assert "project_id:" in mapping and "local_path:" in mapping


def test_offline_320_321_322_real_http_mcp_hermes_and_local_git(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "fixture-repo"
    shas = _fixture_repository(repo)
    visible = {320}
    store = JsonEventStore(tmp_path / "state.json")

    with _serve(_fake_gitlab(shas, visible)) as gitlab_url:
        gitlab_http = httpx.Client(
            base_url=f"{gitlab_url}/api/v4",
            headers={"PRIVATE-TOKEN": "fake-read-only"},
        )
        ingestor = DeploymentIngestor(
            GitLabClient(
                gitlab_http,
                42,
                environment_branches={
                    "dev": "develop",
                    "test": "testing",
                    "stage": "stable",
                    "prod": "master",
                },
            ),
            store,
        )
        baseline = ingestor.poll()
        assert baseline.added == 1
        assert not [event for event in store.load().deployments.values() if event.status == "pending"]

        # Hermes is offline: both rollout transitions are ingested before any MCP claim.
        visible.update({321, 322})
        rollouts = ingestor.poll()
        gitlab_http.close()
        assert rollouts.added == 2

    diff_321 = _git(repo, "diff", "--name-only", shas[0], shas[1]).splitlines()
    diff_322 = _git(repo, "diff", "--name-only", shas[1], shas[2]).splitlines()
    assert diff_321 == ["release.txt"]
    assert diff_322 == ["risk.txt"]
    for sha in shas:
        _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")

    watcher_app = create_app(store, str(tmp_path / "reports"))
    with _serve(watcher_app) as watcher_url:
        hermes = shutil.which("hermes")
        if hermes is not None:
            cli_help = subprocess.run(
                [hermes, "mcp", "add", "--help"],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            assert cli_help.returncode == 0, cli_help.stdout + cli_help.stderr
            assert "--url" in cli_help.stdout

        async def exercise() -> None:
            async with streamable_http_client(f"{watcher_url}/mcp") as streams, ClientSession(
                streams[0], streams[1]
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert tuple(tool.name for tool in tools.tools) == TOOL_NAMES

                listed = _payload(await session.call_tool("list_pending_deployments", {}))
                items = listed["result"]["items"]
                assert [item["deployment_id"] for item in items] == [321, 322]
                assert [item["claimable"] for item in items] == [True, False]
                assert items[0]["previous_sha"] == shas[0]
                assert items[0]["sha"] == shas[1]
                assert items[1]["previous_sha"] == shas[1]
                assert items[1]["sha"] == shas[2]

                first = _payload(
                    await session.call_tool("claim_deployment", {"deployment_id": 321})
                )
                token_321 = first["result"]["claim_token"]
                conflict = _payload(
                    await session.call_tool("claim_deployment", {"deployment_id": 321})
                )
                assert conflict["error"]["code"] == "CLAIM_CONFLICT"
                renewed = _payload(
                    await session.call_tool(
                        "claim_deployment",
                        {"deployment_id": 321, "claim_token": token_321},
                    )
                )
                assert renewed["ok"] is True
                assert renewed["result"]["claim_token"] == token_321
                blocked = _payload(
                    await session.call_tool("claim_deployment", {"deployment_id": 322})
                )
                assert blocked["error"]["code"] == "PREDECESSOR_NOT_COMPLETED"

                context = _payload(
                    await session.call_tool(
                        "get_deployment_context",
                        {"deployment_id": 321, "claim_token": token_321},
                    )
                )
                assert context["result"]["old_sha"] == shas[0]
                assert context["result"]["new_sha"] == shas[1]
                assert str(repo) not in str(context)
                wrong = _payload(
                    await session.call_tool(
                        "get_release_metadata",
                        {"deployment_id": 321, "claim_token": "wrong"},
                    )
                )
                assert wrong["error"]["code"] == "CLAIM_TOKEN_INVALID"
                files = _payload(
                    await session.call_tool(
                        "get_changed_files",
                        {"deployment_id": 321, "claim_token": token_321},
                    )
                )
                assert [item["new_path"] for item in files["result"]["items"]] == [
                    "release.txt"
                ]

                report_321 = _report(321, shas[0], shas[1])
                complete_321 = _payload(
                    await session.call_tool(
                        "mark_analysis_completed",
                        {
                            "deployment_id": 321,
                            "claim_token": token_321,
                            "report_markdown": report_321,
                            "analysis_outcome": "complete",
                        },
                    )
                )
                assert complete_321["ok"] is True
                assert token_321 not in str(complete_321)
                assert report_321 not in str(complete_321)
                retry = _payload(
                    await session.call_tool(
                        "mark_analysis_completed",
                        {
                            "deployment_id": 321,
                            "claim_token": token_321,
                            "report_markdown": report_321,
                            "analysis_outcome": "complete",
                        },
                    )
                )
                assert retry["ok"] is True
                mismatch = _payload(
                    await session.call_tool(
                        "mark_analysis_completed",
                        {
                            "deployment_id": 321,
                            "claim_token": token_321,
                            "report_markdown": report_321 + "changed\n",
                            "analysis_outcome": "complete",
                        },
                    )
                )
                assert mismatch["error"]["code"] == "COMPLETION_PAYLOAD_MISMATCH"

                advanced = _payload(
                    await session.call_tool("list_pending_deployments", {})
                )
                assert [item["deployment_id"] for item in advanced["result"]["items"]] == [
                    322
                ]
                assert advanced["result"]["items"][0]["claimable"] is True
                second = _payload(
                    await session.call_tool("claim_deployment", {"deployment_id": 322})
                )
                token_322 = second["result"]["claim_token"]
                report_322 = _report(322, shas[1], shas[2])
                complete_322 = _payload(
                    await session.call_tool(
                        "mark_analysis_completed",
                        {
                            "deployment_id": 322,
                            "claim_token": token_322,
                            "report_markdown": report_322,
                            "analysis_outcome": "complete",
                        },
                    )
                )
                assert complete_322["ok"] is True
                report = _payload(
                    await session.call_tool(
                        "get_report", {"deployment_id": 322, "max_chars": 40}
                    )
                )
                chunks = [report["result"]["content"]]
                cursor = report["result"]["next_cursor"]
                while cursor is not None:
                    report = _payload(
                        await session.call_tool(
                            "get_report",
                            {
                                "deployment_id": 322,
                                "cursor": cursor,
                                "max_chars": 40,
                            },
                        )
                    )
                    chunks.append(report["result"]["content"])
                    cursor = report["result"]["next_cursor"]
                assert "".join(chunks) == report_322

        asyncio.run(exercise())

    state = store.load()
    assert state.deployments["42:320"].status == "completed"
    assert state.deployments["42:321"].status == "completed"
    assert state.deployments["42:322"].status == "completed"
    state_text = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "claim_token" not in state_text
    assert str(repo) not in state_text
