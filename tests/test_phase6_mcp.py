from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.completion_service import REQUIRED_HEADINGS
from app.context_service import ContextService
from app.mcp_server import TOOL_NAMES, create_app, create_mcp_server
from app.models import (
    ChangedFilePreview,
    CommitSnapshot,
    ComparePreview,
    DeploymentEvent,
    ProjectSnapshot,
    State,
)
from app.storage import JsonEventStore

NOW = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
REPORT = "\n\n".join((*REQUIRED_HEADINGS, "done")) + "\n"


def _store(tmp_path: Path) -> JsonEventStore:
    store = JsonEventStore(tmp_path / "state.json")
    changed = ChangedFilePreview(
        old_path="src/old.py",
        new_path="src/new.py",
        added=False,
        renamed=True,
        deleted=False,
        generated=False,
        collapsed=False,
        too_large=False,
    )
    event = DeploymentEvent(
        project_id=42,
        deployment_id=321,
        environment="test",
        sha="b" * 40,
        previous_sha="a" * 40,
        finished_at=NOW,
        discovered_at=NOW,
        pipeline_id=9001,
        pipeline_ref="test",
        project=ProjectSnapshot(
            gitlab_host="gitlab.example.com",
            project_id=42,
            name="qa-app",
            web_url="https://gitlab.example.com/group/qa-app",
            http_repo_url="https://secret:password@gitlab.example.com/group/qa-app.git?token=x",
            ssh_repo_url=(
                "git:password@gitlab.example.com:group/qa-app.git?private_token=secret"
            ),
        ),
        commit=CommitSnapshot(
            sha="b" * 40,
            title="Release",
            author_name="QA",
            authored_date="2026-07-29T18:00:00Z",
            web_url="https://gitlab.example.com/group/qa-app/-/commit/" + "b" * 40,
        ),
        compare_preview=ComparePreview(
            changed_files=(changed,),
            changed_file_count=1,
            added_files=0,
            renamed_files=1,
            deleted_files=0,
            generated_files=0,
            files_truncated=False,
            compare_timeout=False,
            incomplete=False,
            same_ref=False,
        ),
    )
    store.save(State(deployments={"42:321": event}), expected_revision=0)
    return store


def _payload(result: Any) -> dict[str, Any]:
    payload = result.structuredContent
    assert isinstance(payload, dict)
    return payload


def test_context_projection_redacts_url_credentials_and_requires_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    context = ContextService(store, clock=lambda: NOW)
    unavailable = context.get_deployment_context(321, "wrong")
    assert unavailable["error"]["code"] == "DEPLOYMENT_NOT_PENDING"

    server = create_mcp_server(store, str(tmp_path / "reports"), clock=lambda: NOW)
    assert server is not None


def test_streamable_http_full_seven_tool_workflow_and_transport_security(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = _store(tmp_path)
        app = create_app(store, str(tmp_path / "reports"), clock=lambda: NOW)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as client:
            invalid_host = await client.post(
                "/mcp",
                headers={"host": "attacker.example", "content-type": "application/json"},
                json={},
            )
            invalid_origin = await client.post(
                "/mcp",
                headers={"origin": "https://attacker.example"},
                json={},
            )
            assert invalid_host.status_code == 421
            assert invalid_origin.status_code == 403
            assert "access-control-allow-origin" not in invalid_origin.headers

            async with streamable_http_client(
                "http://localhost/mcp", http_client=client
            ) as streams, ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert tuple(tool.name for tool in tools.tools) == TOOL_NAMES

                listed = _payload(await session.call_tool("list_pending_deployments", {}))
                assert listed["ok"] is True
                assert [item["deployment_id"] for item in listed["result"]["items"]] == [
                    321
                ]

                claimed = _payload(
                    await session.call_tool(
                        "claim_deployment", {"deployment_id": 321}
                    )
                )
                token = claimed["result"]["claim_token"]
                assert token

                context = _payload(
                    await session.call_tool(
                        "get_deployment_context",
                        {"deployment_id": 321, "claim_token": token},
                    )
                )
                serialized_context = str(context)
                assert context["ok"] is True
                assert context["result"]["old_sha"] == "a" * 40
                assert "password" not in serialized_context
                assert "token=x" not in serialized_context
                assert "private_token" not in serialized_context
                assert "secret" not in serialized_context
                assert "ssh://gitlab.example.com/group/qa-app.git" in serialized_context
                assert "'diff':" not in serialized_context.lower()

                changed = _payload(
                    await session.call_tool(
                        "get_changed_files",
                        {"deployment_id": 321, "claim_token": token},
                    )
                )
                assert changed["result"]["items"][0]["new_path"] == "src/new.py"

                release = _payload(
                    await session.call_tool(
                        "get_release_metadata",
                        {"deployment_id": 321, "claim_token": token},
                    )
                )
                assert release["result"]["pipeline"]["id"] == 9001

                completed = _payload(
                    await session.call_tool(
                        "mark_analysis_completed",
                        {
                            "deployment_id": 321,
                            "claim_token": token,
                            "report_markdown": REPORT,
                            "analysis_outcome": "complete",
                        },
                    )
                )
                assert completed["ok"] is True
                assert token not in str(completed)
                assert REPORT not in str(completed)

                report = _payload(
                    await session.call_tool(
                        "get_report", {"deployment_id": 321}
                    )
                )
                assert report["ok"] is True
                assert report["result"]["content"] == REPORT

    asyncio.run(exercise())


def test_streamable_http_requires_project_id_for_colliding_deployment_ids(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = _store(tmp_path)
        state = store.load()
        first = state.deployments["42:321"]
        second = first.model_copy(
            update={
                "project_id": 43,
                "sha": "c" * 40,
                "project": None,
            }
        )
        store.save(
            State(
                revision=state.revision,
                deployments={**state.deployments, "43:321": second},
            ),
            expected_revision=state.revision,
        )
        app = create_app(store, str(tmp_path / "reports"), clock=lambda: NOW)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as client, streamable_http_client(
            "http://localhost/mcp", http_client=client
        ) as streams, ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            listed = _payload(await session.call_tool("list_pending_deployments", {}))
            assert [
                (item["project_id"], item["deployment_id"])
                for item in listed["result"]["items"]
            ] == [(42, 321), (43, 321)]

            ambiguous = _payload(
                await session.call_tool("claim_deployment", {"deployment_id": 321})
            )
            assert ambiguous["error"]["code"] == "AMBIGUOUS_DEPLOYMENT_ID"

            for project_id in (42, 43):
                claimed = _payload(
                    await session.call_tool(
                        "claim_deployment",
                        {"project_id": project_id, "deployment_id": 321},
                    )
                )
                assert claimed["ok"] is True
                assert claimed["result"]["project_id"] == project_id

    asyncio.run(exercise())
