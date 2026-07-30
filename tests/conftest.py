from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_gitlab_client() -> httpx.Client:
    deployments = json.loads((FIXTURES / "deployments.json").read_text())
    pipelines = json.loads((FIXTURES / "pipelines.json").read_text())
    compares = json.loads((FIXTURES / "compare.json").read_text())
    project = json.loads((FIXTURES / "project.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/projects/42/deployments":
            assert request.url.params["status"] == "success"
            assert request.url.params["per_page"] == "100"
            page = request.url.params["page"]
            if page == "1":
                return httpx.Response(
                    200, json=deployments[:2], headers={"X-Next-Page": "2"}
                )
            assert page == "2"
            return httpx.Response(
                200, json=deployments[2:], headers={"X-Next-Page": ""}
            )
        if path.startswith("/api/v4/projects/42/pipelines/"):
            return httpx.Response(200, json=pipelines[path.rsplit("/", 1)[-1]])
        if path.startswith("/api/v4/projects/42/repository/commits/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": sha,
                    "title": f"Deploy {sha[:3]}",
                    "author_name": "Fixture Author",
                    "authored_date": "2026-01-01T00:00:00Z",
                    "web_url": f"https://gitlab.test/group/project/-/commit/{sha}",
                },
            )
        if path == "/api/v4/projects/42/repository/compare":
            query = parse_qs(request.url.query.decode())
            assert query["straight"] == ["true"]
            return httpx.Response(200, json=compares[f"{query['from'][0]}..{query['to'][0]}"])
        if path == "/api/v4/projects/42":
            return httpx.Response(200, json=project)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.Client(
        base_url="https://gitlab.test/api/v4",
        transport=httpx.MockTransport(handler),
        headers={"PRIVATE-TOKEN": "fixture-token"},
        follow_redirects=False,
    )
