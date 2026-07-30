from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.gitlab_client import (
    CompareResult,
    DeploymentRejection,
    GitLabClient,
    GitLabProtocolError,
)


def _client(
    handler: Any,
    *,
    follow_redirects: bool = True,
    environment_branches: dict[str, str] | None = None,
) -> GitLabClient:
    http = httpx.Client(
        base_url="https://gitlab.test/api/v4",
        headers={"PRIVATE-TOKEN": "secret"},
        transport=httpx.MockTransport(handler),
        follow_redirects=follow_redirects,
    )
    return GitLabClient(
        http, project_id=42, environment_branches=environment_branches
    )


def _deployment(
    deployment_id: int,
    sha: str,
    *,
    status: str = "success",
    deployable_status: str | None = "success",
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    deployable: dict[str, Any] = {}
    if deployable_status is not None:
        deployable["status"] = deployable_status
    if pipeline_id is not None:
        deployable["pipeline"] = {"id": pipeline_id}
    return {
        "id": deployment_id,
        "status": status,
        "sha": sha,
        "finished_at": "2026-07-29T17:20:00Z",
        "environment": {"name": "test"},
        "deployable": deployable,
    }


def test_deployments_paginate_and_sort_by_finished_at_then_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(
                200,
                json=[
                    {**_deployment(4, "d", pipeline_id=14), "finished_at": "2026-07-29T18:00:00Z"},
                    {**_deployment(3, "c", pipeline_id=13), "finished_at": "2026-07-29T17:00:00Z"},
                ],
                headers={"X-Next-Page": "2"},
            )
        return httpx.Response(
            200,
            json=[
                {**_deployment(2, "b", pipeline_id=12), "finished_at": "2026-07-29T17:00:00Z"}
            ],
            headers={"X-Next-Page": ""},
        )

    result = _client(handler).deployments(
        overlap_finished_at="2026-07-29T16:00:00Z", overlap_deployment_id=1
    )

    assert [item["id"] for item in result] == [2, 3, 4]
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert all(request.url.params["status"] == "success" for request in requests)


def test_recent_deployments_is_single_page_environment_bounded_replay_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {**_deployment(3, "c", pipeline_id=13), "finished_at": "2026-07-29T19:00:00Z"},
                {**_deployment(2, "b", pipeline_id=12), "finished_at": "2026-07-29T18:00:00Z"},
                {**_deployment(1, "a", pipeline_id=11), "finished_at": "2026-07-29T17:00:00Z"},
            ],
            headers={"X-Next-Page": "99"},
        )

    result = _client(handler).recent_deployments(environment="test", limit=3)

    assert [item["id"] for item in result] == [1, 2, 3]
    assert len(requests) == 1
    assert requests[0].url.params["environment"] == "test"
    assert requests[0].url.params["sort"] == "desc"
    assert requests[0].url.params["per_page"] == "3"


@pytest.mark.parametrize("next_page", ["1", "not-a-page"])
def test_deployments_reject_invalid_or_cyclic_pagination(next_page: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"X-Next-Page": next_page})

    with pytest.raises(GitLabProtocolError, match="pagination"):
        _client(handler).deployments()


@pytest.mark.parametrize("endpoint", ["deployments", "pipeline", "project", "commit"])
def test_successful_non_json_responses_use_safe_protocol_error(endpoint: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="sensitive non-json body")

    client = _client(handler)
    with pytest.raises(GitLabProtocolError, match="valid JSON") as caught:
        if endpoint == "deployments":
            client.deployments()
        elif endpoint == "pipeline":
            client.pipeline(1)
        elif endpoint == "project":
            client.project_metadata()
        else:
            client.commit_metadata("abc")
    assert "sensitive non-json body" not in str(caught.value)


@pytest.mark.parametrize(
    ("deployment", "pipeline", "reason"),
    [
        (_deployment(1, "a", status="failed", pipeline_id=11), None, "deployment_not_success"),
        (_deployment(1, "a", deployable_status="failed", pipeline_id=11), None, "deployable_not_success"),
        (_deployment(1, "a"), None, "canonical_pipeline_missing"),
        (
            _deployment(1, "a", pipeline_id=11),
            {"id": 12, "status": "success", "sha": "a"},
            "pipeline_id_mismatch",
        ),
        (_deployment(1, "a", pipeline_id=11), {"id": 11, "status": "failed", "sha": "a"}, "pipeline_not_success"),
        (_deployment(1, "a", pipeline_id=11), {"id": 11, "status": "success", "sha": "b"}, "sha_mismatch"),
    ],
)
def test_verify_deployment_has_safe_exact_rejections(
    deployment: dict[str, Any],
    pipeline: dict[str, Any] | None,
    reason: str,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert pipeline is not None
        return httpx.Response(200, json=pipeline)

    result = _client(handler).verify_deployment(deployment)

    assert result.accepted is False
    assert result.rejection is DeploymentRejection(reason)
    assert "secret" not in repr(result)
    if pipeline is None:
        assert paths == []


def test_only_nested_canonical_pipeline_id_is_used_despite_duplicate_sha_pipelines() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path.endswith("/pipelines/99")
        return httpx.Response(
            200, json={"id": 99, "status": "success", "sha": "same", "ref": "test"}
        )

    result = _client(handler).verify_deployment(_deployment(9, "same", pipeline_id=99))

    assert result.accepted is True
    assert result.deployment is not None
    assert result.deployment.pipeline_ref == "test"
    assert paths == ["/api/v4/projects/42/pipelines/99"]


def test_verify_deployment_requires_exact_environment_branch_ref_when_configured() -> None:
    pipeline_ref = "testing"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 99,
                "status": "success",
                "sha": "same",
                "ref": pipeline_ref,
            },
        )

    client = _client(
        handler,
        environment_branches={
            "dev": "develop",
            "test": "testing",
            "stage": "stable",
            "prod": "master",
        },
    )
    accepted = client.verify_deployment(_deployment(9, "same", pipeline_id=99))
    pipeline_ref = "test"
    rejected = client.verify_deployment(_deployment(10, "same", pipeline_id=99))

    assert accepted.accepted is True
    assert rejected.accepted is False
    assert rejected.rejection is DeploymentRejection.PIPELINE_REF_MISMATCH


def test_newest_invalid_candidates_do_not_hide_older_verified_candidates() -> None:
    deployments = [
        {**_deployment(1, "old", pipeline_id=11), "finished_at": "2026-07-29T17:00:00Z"},
        {**_deployment(2, "new", pipeline_id=12), "finished_at": "2026-07-29T18:00:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        pipeline_id = int(request.url.path.rsplit("/", 1)[-1])
        status = "success" if pipeline_id == 11 else "failed"
        return httpx.Response(
            200,
            json={
                "id": pipeline_id,
                "status": status,
                "sha": "old" if pipeline_id == 11 else "new",
                "ref": "test",
            },
        )

    verified = _client(handler).verify_deployments(deployments)

    assert [item.deployment_id for item in verified.accepted] == [1]
    assert verified.rejected[0].deployment_id == 2


def test_redirect_is_never_followed_and_cross_origin_location_is_rejected() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    with pytest.raises(GitLabProtocolError, match="cross-origin redirect refused"):
        _client(handler).pipeline(1)
    assert calls == 1


def test_project_metadata_sanitizes_repository_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 42,
                "name": "safe",
                "web_url": "https://gitlab.test/group/safe",
                "http_url_to_repo": "https://user:password@gitlab.test/group/safe.git?token=x#f",
                "ssh_url_to_repo": "ssh://git:password@gitlab.test/group/safe.git?x=1#f",
            },
        )

    metadata = _client(handler).project_metadata()

    assert metadata.gitlab_host == "gitlab.test"
    assert metadata.http_repo_url == "https://gitlab.test/group/safe.git"
    assert metadata.ssh_repo_url == "ssh://gitlab.test/group/safe.git"
    assert "password" not in repr(metadata)


@pytest.mark.parametrize("url", ["not a url", "https://user@:bad/x", "file:///tmp/repo"])
def test_project_metadata_rejects_malformed_or_unsafe_repo_urls(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 42,
                "name": "unsafe",
                "web_url": "https://gitlab.test/group/unsafe",
                "http_url_to_repo": url,
                "ssh_url_to_repo": "git@gitlab.test:group/unsafe.git",
            },
        )

    with pytest.raises(GitLabProtocolError):
        _client(handler).project_metadata()


def test_compare_strips_raw_diff_and_bounds_factual_flags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["straight"] == "true"
        return httpx.Response(
            200,
            json={
                "compare_timeout": False,
                "compare_same_ref": False,
                "diffs": [
                    {
                        "old_path": "old.py",
                        "new_path": "new.py",
                        "new_file": False,
                        "renamed_file": True,
                        "deleted_file": False,
                        "generated_file": True,
                        "diff": "TOP SECRET PATCH",
                        "a_mode": "100644",
                        "unknown": {"raw": "discard me"},
                    }
                ],
            },
        )

    result = _client(handler).compare("old", "new")

    assert isinstance(result, CompareResult)
    assert result.incomplete is False
    assert result.changed_file_count == 1
    assert result.changed_files[0].renamed is True
    assert result.changed_files[0].generated is True
    assert result.stats.renamed_files == 1
    assert result.stats.generated_files == 1
    assert "TOP SECRET" not in result.model_dump_json()
    assert "a_mode" not in result.model_dump_json()


def test_compare_timeout_and_http_failure_are_explicitly_incomplete() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"compare_timeout": True, "diffs": []}),
            httpx.Response(503, json={"message": "secret backend error"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _client(handler)
    timeout = client.compare("a", "b")
    failure = client.compare("b", "c")

    assert timeout.compare_timeout is True and timeout.incomplete is True
    assert failure.incomplete is True
    assert failure.failure == "http_503"
    assert failure.no_changes is False
    assert "secret backend error" not in repr(failure)


def test_commit_metadata_is_exact_and_has_no_unknown_response_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 17,
                        "title": "Release  MR",
                        "description": "Ship release\n\nKeep  details",
                        "merge_commit_sha": "abc",
                        "squash_commit_sha": None,
                        "unknown": "discard",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "title": "A commit",
                "author_name": "Ada",
                "authored_date": "2026-07-29T17:00:00Z",
                "web_url": "https://gitlab.test/group/project/-/commit/abc",
                "message": "discard",
            },
        )

    metadata = _client(handler).commit_metadata("abc")
    assert metadata.model_dump() == {
        "sha": "abc",
        "title": "A commit",
        "author_name": "Ada",
        "authored_date": "2026-07-29T17:00:00Z",
        "web_url": "https://gitlab.test/group/project/-/commit/abc",
        "merge_request_title": "Release  MR",
        "merge_request_description": "Ship release\n\nKeep  details",
    }


def test_commit_metadata_uses_fallback_when_related_merge_request_is_not_exact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "title": "Unrelated MR",
                        "description": "Must not be selected",
                        "merge_commit_sha": "different",
                        "squash_commit_sha": None,
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "title": "Fallback commit title",
                "author_name": "Ada",
                "authored_date": "2026-07-29T17:00:00Z",
                "web_url": "https://gitlab.test/group/project/-/commit/abc",
            },
        )

    metadata = _client(handler).commit_metadata("abc")

    assert metadata.title == "Fallback commit title"
    assert metadata.merge_request_title is None
    assert metadata.merge_request_description is None


def test_commit_metadata_uses_fallback_for_incomplete_merge_request_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/merge_requests"):
            assert dict(request.url.params) == {"page": "1", "per_page": "100"}
            return httpx.Response(
                200,
                json=[
                    {
                        "title": "Incomplete exact MR",
                        "description": "Another page exists",
                        "merge_commit_sha": "abc",
                        "squash_commit_sha": None,
                    }
                ],
                headers={"X-Next-Page": "2"},
            )
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "title": "Fallback commit title",
                "author_name": "Ada",
                "authored_date": "2026-07-29T17:00:00Z",
                "web_url": "https://gitlab.test/group/project/-/commit/abc",
            },
        )

    metadata = _client(handler).commit_metadata("abc")

    assert metadata.merge_request_title is None
    assert metadata.merge_request_description is None


def test_commit_metadata_uses_fallback_when_merge_request_entry_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "title": "Exact MR",
                        "description": "Must not be selected",
                        "merge_commit_sha": "abc",
                        "squash_commit_sha": None,
                    },
                    {"title": "Unknown association schema"},
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "title": "Fallback commit title",
                "author_name": "Ada",
                "authored_date": "2026-07-29T17:00:00Z",
                "web_url": "https://gitlab.test/group/project/-/commit/abc",
            },
        )

    metadata = _client(handler).commit_metadata("abc")

    assert metadata.title == "Fallback commit title"
    assert metadata.merge_request_title is None
    assert metadata.merge_request_description is None


def test_commit_metadata_rejects_response_for_another_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/merge_requests")
        return httpx.Response(
            200,
            json={
                "id": "different-sha",
                "title": "Wrong commit",
                "author_name": "Ada",
                "authored_date": "2026-07-29T17:00:00Z",
                "web_url": "https://gitlab.test/group/project/-/commit/different-sha",
            },
        )

    with pytest.raises(GitLabProtocolError, match="commit SHA does not match"):
        _client(handler).commit_metadata("expected-sha")
