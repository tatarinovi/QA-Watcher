from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.gitlab_client import GitLabClient
from app.replay import prepare_replay
from app.storage import JsonEventStore


def _deployment(deployment_id: int, sha: str, pipeline_id: int) -> dict[str, Any]:
    return {
        "id": deployment_id,
        "status": "success",
        "sha": sha,
        "environment": {"name": "test"},
        "deployable": {
            "status": "success",
            "finished_at": f"2026-07-29T1{deployment_id}:00:00Z",
            "pipeline": {"id": pipeline_id},
        },
    }


def test_prepare_replay_uses_real_adapter_and_builds_isolated_fifo(tmp_path: Path) -> None:
    shas = {1: "a" * 40, 2: "b" * 40, 3: "c" * 40, 4: "c" * 40}
    deployments = [_deployment(item, shas[item], 100 + item) for item in (4, 3, 2, 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/deployments"):
            assert request.url.params["environment"] == "test"
            assert request.url.params["sort"] == "desc"
            return httpx.Response(200, json=deployments)
        if "/pipelines/" in path:
            pipeline_id = int(path.rsplit("/", 1)[-1])
            deployment_id = pipeline_id - 100
            return httpx.Response(
                200,
                json={
                    "id": pipeline_id,
                    "status": "success",
                    "sha": shas[deployment_id],
                    "ref": "testing",
                },
            )
        if path.endswith("/projects/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "name": "real-shape-project",
                    "web_url": "https://gitlab.test/group/project",
                    "http_url_to_repo": "https://gitlab.test/group/project.git",
                    "ssh_url_to_repo": "git@gitlab.test:group/project.git",
                },
            )
        if "/repository/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": sha,
                    "title": "Replay commit",
                    "author_name": "QA",
                    "authored_date": "2026-07-29T10:00:00Z",
                    "web_url": f"https://gitlab.test/group/project/-/commit/{sha}",
                },
            )
        if path.endswith("/repository/compare"):
            return httpx.Response(
                200,
                json={
                    "compare_timeout": False,
                    "compare_same_ref": False,
                    "diffs": [
                        {
                            "old_path": "release.txt",
                            "new_path": "release.txt",
                            "new_file": False,
                            "renamed_file": False,
                            "deleted_file": False,
                            "generated_file": False,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    http = httpx.Client(
        base_url="https://gitlab.test/api/v4",
        headers={"PRIVATE-TOKEN": "test-only"},
        transport=httpx.MockTransport(handler),
    )
    client = GitLabClient(
        http,
        42,
        environment_branches={
            "dev": "develop",
            "test": "testing",
            "stage": "stable",
            "prod": "master",
        },
    )
    store = JsonEventStore(tmp_path / "replay" / "state.json")

    summary = prepare_replay(client, store, environment="test", count=3)

    state = store.load()
    assert summary.project_id == 42
    assert summary.deployment_ids == (1, 2, 4)
    assert state.deployments["42:1"].status == "completed"
    assert state.deployments["42:2"].status == "pending"
    assert state.deployments["42:4"].status == "pending"
    assert state.deployments["42:2"].previous_sha == shas[1]
    assert state.deployments["42:4"].previous_sha == shas[2]
    http.close()
