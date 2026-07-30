from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

MAX_CHANGED_FILES = 200
MAX_DEPLOYMENT_PAGES = 100
MAX_MERGE_REQUEST_TITLE = 500
MAX_MERGE_REQUEST_DESCRIPTION = 2500


class GitLabProtocolError(RuntimeError):
    """A secret-safe error for an invalid or unsafe GitLab response."""


class DeploymentRejection(StrEnum):
    DEPLOYMENT_NOT_SUCCESS = "deployment_not_success"
    DEPLOYABLE_NOT_SUCCESS = "deployable_not_success"
    CANONICAL_PIPELINE_MISSING = "canonical_pipeline_missing"
    PIPELINE_ID_MISMATCH = "pipeline_id_mismatch"
    PIPELINE_NOT_SUCCESS = "pipeline_not_success"
    SHA_MISMATCH = "sha_mismatch"
    PIPELINE_REF_MISMATCH = "pipeline_ref_mismatch"
    MALFORMED_RESPONSE = "malformed_response"


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerifiedDeployment(_ContractModel):
    deployment_id: int = Field(gt=0)
    environment: str = Field(min_length=1, max_length=255)
    sha: str = Field(min_length=1, max_length=128)
    finished_at: str
    pipeline_id: int = Field(gt=0)
    pipeline_ref: str = Field(min_length=1, max_length=255)


class DeploymentVerification(_ContractModel):
    deployment_id: int | None
    accepted: bool
    deployment: VerifiedDeployment | None = None
    rejection: DeploymentRejection | None = None


class DeploymentVerificationBatch(_ContractModel):
    accepted: tuple[VerifiedDeployment, ...]
    rejected: tuple[DeploymentVerification, ...]


class ProjectMetadata(_ContractModel):
    gitlab_host: str
    project_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    web_url: str
    http_repo_url: str
    ssh_repo_url: str


class ChangedFile(_ContractModel):
    old_path: str
    new_path: str
    added: bool
    renamed: bool
    deleted: bool
    generated: bool
    collapsed: bool
    too_large: bool


class CompareStats(_ContractModel):
    added_files: int = Field(ge=0)
    renamed_files: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    generated_files: int = Field(ge=0)


class CompareResult(_ContractModel):
    old_sha: str
    new_sha: str
    changed_files: tuple[ChangedFile, ...]
    changed_file_count: int = Field(ge=0)
    stats: CompareStats
    files_truncated: bool
    compare_timeout: bool
    incomplete: bool
    same_ref: bool
    failure: str | None = None

    @property
    def no_changes(self) -> bool:
        return (
            not self.incomplete
            and not self.compare_timeout
            and not self.files_truncated
            and self.changed_file_count == 0
        )


class CommitMetadata(_ContractModel):
    sha: str
    title: str
    author_name: str
    authored_date: str
    web_url: str
    merge_request_title: str | None = None
    merge_request_description: str | None = None


class GitLabClient:
    def __init__(
        self,
        client: httpx.Client,
        project_id: int,
        *,
        environment_branches: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self.project_id = project_id
        base = urlsplit(str(client.base_url))
        if base.scheme not in {"http", "https"} or not base.hostname:
            raise ValueError("GitLab client requires an HTTP(S) base URL with a host")
        self._origin = (base.scheme.lower(), base.hostname.lower(), base.port)
        self._gitlab_host = base.hostname
        self._environment_branches = dict(environment_branches or {})

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        allow_http_failure: bool = False,
    ) -> httpx.Response:
        try:
            response = self._client.get(path, params=params, follow_redirects=False)
        except httpx.HTTPError as exc:
            if allow_http_failure:
                raise
            raise GitLabProtocolError(f"GitLab request failed: {type(exc).__name__}") from None
        if response.is_redirect:
            location = response.headers.get("location", "")
            target = response.url.join(location)
            parsed = urlsplit(str(target))
            target_origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)
            if target_origin != self._origin:
                raise GitLabProtocolError("cross-origin redirect refused")
            raise GitLabProtocolError("redirect refused")
        if not allow_http_failure:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                raise GitLabProtocolError(f"GitLab HTTP {response.status_code}") from None
        return response

    def deployments(
        self,
        *,
        overlap_finished_at: str | datetime | None = None,
        overlap_deployment_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all deployment facts in stable order.

        The overlap cursor is deliberately accepted at the adapter boundary for
        pollers. It does not mutate state and is not trusted as a completeness
        boundary: GitLab pagination is always exhausted.
        """

        if overlap_deployment_id is not None and overlap_deployment_id < 1:
            raise ValueError("overlap_deployment_id must be positive")
        if isinstance(overlap_finished_at, datetime):
            overlap_finished_at.isoformat()
        elif overlap_finished_at is not None:
            datetime.fromisoformat(overlap_finished_at)

        page = "1"
        seen_pages: set[str] = set()
        deployments: list[dict[str, Any]] = []
        while page:
            if page in seen_pages or not page.isdigit() or int(page) < 1:
                raise GitLabProtocolError("deployment pagination is invalid or cyclic")
            if len(seen_pages) >= MAX_DEPLOYMENT_PAGES:
                raise GitLabProtocolError("deployment pagination exceeds safe page limit")
            seen_pages.add(page)
            response = self._get(
                f"/projects/{self.project_id}/deployments",
                params={
                    "status": "success",
                    "order_by": "finished_at",
                    "sort": "asc",
                    "per_page": "100",
                    "page": page,
                },
            )
            payload = _response_json(response, "deployments")
            if not isinstance(payload, list):
                raise GitLabProtocolError("deployments response must be a list")
            if not all(isinstance(item, dict) for item in payload):
                raise GitLabProtocolError("deployment entry must be an object")
            deployments.extend(dict(item) for item in payload)
            page = response.headers.get("X-Next-Page", "").strip()
        try:
            return sorted(
                deployments,
                key=lambda item: (
                    datetime.fromisoformat(_deployment_finished_at(item)),
                    int(item["id"]),
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise GitLabProtocolError("deployment ordering fields are malformed") from None

    def recent_deployments(
        self, *, environment: str, limit: int
    ) -> list[dict[str, Any]]:
        """Return one bounded real-GitLab page for an explicit local replay."""

        if not environment.strip():
            raise ValueError("environment must not be empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        response = self._get(
            f"/projects/{self.project_id}/deployments",
            params={
                "status": "success",
                "environment": environment,
                "order_by": "finished_at",
                "sort": "desc",
                "per_page": str(limit),
                "page": "1",
            },
        )
        payload = _response_json(response, "deployments")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise GitLabProtocolError("deployments response must be a list of objects")
        try:
            return sorted(
                (dict(item) for item in payload),
                key=lambda item: (
                    datetime.fromisoformat(_deployment_finished_at(item)),
                    int(item["id"]),
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise GitLabProtocolError("deployment ordering fields are malformed") from None

    def pipeline(self, pipeline_id: int) -> dict[str, Any]:
        response = self._get(f"/projects/{self.project_id}/pipelines/{pipeline_id}")
        payload = _response_json(response, "pipeline")
        if not isinstance(payload, dict):
            raise GitLabProtocolError("pipeline response must be an object")
        return dict(payload)

    def verify_deployment(self, deployment: dict[str, Any]) -> DeploymentVerification:
        deployment_id = _safe_positive_int(deployment.get("id"))
        if deployment.get("status") != "success":
            return _rejected(deployment_id, DeploymentRejection.DEPLOYMENT_NOT_SUCCESS)
        deployable = deployment.get("deployable")
        if not isinstance(deployable, dict):
            return _rejected(deployment_id, DeploymentRejection.CANONICAL_PIPELINE_MISSING)
        deployable_status = deployable.get("status")
        if deployable_status is not None and deployable_status != "success":
            return _rejected(deployment_id, DeploymentRejection.DEPLOYABLE_NOT_SUCCESS)
        pipeline_ref = deployable.get("pipeline")
        pipeline_id = (
            _safe_positive_int(pipeline_ref.get("id")) if isinstance(pipeline_ref, dict) else None
        )
        if pipeline_id is None:
            return _rejected(deployment_id, DeploymentRejection.CANONICAL_PIPELINE_MISSING)
        pipeline = self.pipeline(pipeline_id)
        if _safe_positive_int(pipeline.get("id")) != pipeline_id:
            return _rejected(deployment_id, DeploymentRejection.PIPELINE_ID_MISMATCH)
        if pipeline.get("status") != "success":
            return _rejected(deployment_id, DeploymentRejection.PIPELINE_NOT_SUCCESS)
        sha = deployment.get("sha")
        if not isinstance(sha, str) or not sha or pipeline.get("sha") != sha:
            return _rejected(deployment_id, DeploymentRejection.SHA_MISMATCH)
        environment = deployment.get("environment")
        if deployment_id is None or not isinstance(environment, dict):
            return _rejected(deployment_id, DeploymentRejection.MALFORMED_RESPONSE)
        environment_name = environment.get("name")
        if not isinstance(environment_name, str) or not environment_name:
            return _rejected(deployment_id, DeploymentRejection.MALFORMED_RESPONSE)
        canonical_environment = _canonical_environment(environment_name)
        expected_ref = (
            self._environment_branches.get(canonical_environment)
            if canonical_environment is not None
            else None
        )
        if expected_ref is not None and pipeline.get("ref") != expected_ref:
            return _rejected(deployment_id, DeploymentRejection.PIPELINE_REF_MISMATCH)
        try:
            exact_pipeline_ref = pipeline.get("ref")
            if not isinstance(exact_pipeline_ref, str) or not exact_pipeline_ref:
                raise ValueError
            verified = VerifiedDeployment(
                deployment_id=deployment_id,
                environment=environment_name,
                sha=sha,
                finished_at=_deployment_finished_at(deployment),
                pipeline_id=pipeline_id,
                pipeline_ref=exact_pipeline_ref,
            )
        except (KeyError, TypeError, ValueError):
            return _rejected(deployment_id, DeploymentRejection.MALFORMED_RESPONSE)
        return DeploymentVerification(
            deployment_id=deployment_id, accepted=True, deployment=verified
        )

    def verify_deployments(
        self, deployments: list[dict[str, Any]]
    ) -> DeploymentVerificationBatch:
        results = [self.verify_deployment(item) for item in deployments]
        accepted = tuple(
            result.deployment
            for result in results
            if result.accepted and result.deployment is not None
        )
        rejected = tuple(result for result in results if not result.accepted)
        return DeploymentVerificationBatch(accepted=accepted, rejected=rejected)

    def project_metadata(self) -> ProjectMetadata:
        response = self._get(f"/projects/{self.project_id}")
        payload = _response_json(response, "project")
        if not isinstance(payload, dict):
            raise GitLabProtocolError("project response must be an object")
        try:
            project_id = int(payload["id"])
            if project_id != self.project_id:
                raise GitLabProtocolError("project ID does not match request")
            return ProjectMetadata(
                gitlab_host=self._gitlab_host,
                project_id=project_id,
                name=str(payload["name"]),
                web_url=_safe_public_url(str(payload["web_url"]), {"http", "https"}),
                http_repo_url=_safe_public_url(
                    str(payload["http_url_to_repo"]), {"http", "https"}
                ),
                ssh_repo_url=_safe_public_url(
                    str(payload["ssh_url_to_repo"]), {"ssh"}, allow_scp=True
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise GitLabProtocolError("project metadata is malformed") from None

    def compare(self, previous_sha: str, sha: str) -> CompareResult:
        try:
            response = self._get(
                f"/projects/{self.project_id}/repository/compare",
                params={"from": previous_sha, "to": sha, "straight": "true"},
                allow_http_failure=True,
            )
        except httpx.HTTPError as exc:
            return _incomplete_compare(previous_sha, sha, f"transport_{type(exc).__name__}")
        if response.status_code >= 400:
            return _incomplete_compare(previous_sha, sha, f"http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return _incomplete_compare(previous_sha, sha, "invalid_json")
        if not isinstance(payload, dict):
            return _incomplete_compare(previous_sha, sha, "invalid_schema")
        raw_diffs = payload.get("diffs", [])
        if not isinstance(raw_diffs, list):
            return _incomplete_compare(previous_sha, sha, "invalid_schema")
        changed_files: list[ChangedFile] = []
        for raw in raw_diffs[:MAX_CHANGED_FILES]:
            if not isinstance(raw, dict):
                return _incomplete_compare(previous_sha, sha, "invalid_schema")
            try:
                changed_files.append(
                    ChangedFile(
                        old_path=str(raw["old_path"]),
                        new_path=str(raw["new_path"]),
                        added=raw.get("new_file") is True,
                        renamed=raw.get("renamed_file") is True,
                        deleted=raw.get("deleted_file") is True,
                        generated=raw.get("generated_file") is True,
                        collapsed=raw.get("collapsed") is True,
                        too_large=raw.get("too_large") is True,
                    )
                )
            except KeyError:
                return _incomplete_compare(previous_sha, sha, "invalid_schema")
        timeout = payload.get("compare_timeout") is True
        truncated = len(raw_diffs) > MAX_CHANGED_FILES
        return CompareResult(
            old_sha=previous_sha,
            new_sha=sha,
            changed_files=tuple(changed_files),
            changed_file_count=len(raw_diffs),
            stats=CompareStats(
                added_files=sum(item.added for item in changed_files),
                renamed_files=sum(item.renamed for item in changed_files),
                deleted_files=sum(item.deleted for item in changed_files),
                generated_files=sum(item.generated for item in changed_files),
            ),
            files_truncated=truncated,
            compare_timeout=timeout,
            incomplete=timeout or truncated,
            same_ref=payload.get("compare_same_ref") is True,
        )

    def commit_metadata(self, sha: str) -> CommitMetadata:
        response = self._get(f"/projects/{self.project_id}/repository/commits/{sha}")
        payload = _response_json(response, "commit")
        if not isinstance(payload, dict):
            raise GitLabProtocolError("commit response must be an object")
        try:
            commit_sha = str(payload["id"])
            title = str(payload["title"])
            author_name = str(payload["author_name"])
            authored_date = str(payload["authored_date"])
            web_url = _safe_public_url(str(payload["web_url"]), {"http", "https"})
        except (KeyError, TypeError, ValueError):
            raise GitLabProtocolError("commit metadata is malformed") from None
        if commit_sha != sha:
            raise GitLabProtocolError("commit SHA does not match requested SHA")
        mr_title, mr_description = self._exact_merge_request(commit_sha)
        return CommitMetadata(
            sha=commit_sha,
            title=title,
            author_name=author_name,
            authored_date=authored_date,
            web_url=web_url,
            merge_request_title=mr_title,
            merge_request_description=mr_description,
        )

    def _exact_merge_request(self, sha: str) -> tuple[str | None, str | None]:
        try:
            response = self._get(
                f"/projects/{self.project_id}/repository/commits/{sha}/merge_requests",
                params={"page": "1", "per_page": "100"},
                allow_http_failure=True,
            )
        except (httpx.HTTPError, GitLabProtocolError):
            return None, None
        if response.status_code >= 400:
            return None, None
        if response.headers.get("X-Next-Page", "").strip():
            return None, None
        try:
            payload = response.json()
        except ValueError:
            return None, None
        if not isinstance(payload, list):
            return None, None
        for item in payload:
            if not isinstance(item, dict):
                return None, None
            if "merge_commit_sha" not in item or "squash_commit_sha" not in item:
                return None, None
            association_shas = (item["merge_commit_sha"], item["squash_commit_sha"])
            if any(
                value is not None and (not isinstance(value, str) or not value)
                for value in association_shas
            ):
                return None, None
        exact = [
            item
            for item in payload
            if item["merge_commit_sha"] == sha or item["squash_commit_sha"] == sha
        ]
        if len(exact) != 1:
            return None, None
        title = exact[0].get("title")
        description = exact[0].get("description")
        if not isinstance(title, str) or not title.strip():
            return None, None
        return (
            title[:MAX_MERGE_REQUEST_TITLE],
            description[:MAX_MERGE_REQUEST_DESCRIPTION]
            if isinstance(description, str) and description.strip()
            else None,
        )


def _response_json(response: httpx.Response, resource: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise GitLabProtocolError(f"{resource} response must contain valid JSON") from None


def _safe_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | str | bytes | bytearray):
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _deployment_finished_at(deployment: Mapping[str, Any]) -> str:
    """Read the factual finish time across supported GitLab deployment shapes."""

    deployable = deployment.get("deployable")
    candidates = (
        deployment.get("finished_at"),
        deployable.get("finished_at") if isinstance(deployable, Mapping) else None,
    )
    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        datetime.fromisoformat(value)
        return value
    raise ValueError("deployment finished_at is missing or malformed")


def _canonical_environment(value: str) -> str | None:
    return {
        "dev": "dev",
        "development": "dev",
        "test": "test",
        "testing": "test",
        "stage": "stage",
        "staging": "stage",
        "prod": "prod",
        "production": "prod",
    }.get(value.strip().casefold())


def _rejected(
    deployment_id: int | None, reason: DeploymentRejection
) -> DeploymentVerification:
    return DeploymentVerification(
        deployment_id=deployment_id, accepted=False, rejection=reason
    )


def _incomplete_compare(old_sha: str, new_sha: str, failure: str) -> CompareResult:
    return CompareResult(
        old_sha=old_sha,
        new_sha=new_sha,
        changed_files=(),
        changed_file_count=0,
        stats=CompareStats(
            added_files=0,
            renamed_files=0,
            deleted_files=0,
            generated_files=0,
        ),
        files_truncated=False,
        compare_timeout=False,
        incomplete=True,
        same_ref=False,
        failure=failure,
    )


def _safe_public_url(
    value: str, allowed_schemes: set[str], *, allow_scp: bool = False
) -> str:
    if allow_scp and "://" not in value and ":" in value:
        authority, path = value.split(":", 1)
        host = authority.rsplit("@", 1)[-1]
        if not host or not path or any(char in host for char in "/?#:"):
            raise GitLabProtocolError("repository URL is malformed")
        value = f"ssh://{host}/{path}"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise GitLabProtocolError("repository URL is malformed") from None
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not parsed.hostname
        or not parsed.path
        or parsed.hostname.lower() in {"localhost"}
    ):
        raise GitLabProtocolError("repository URL is unsafe")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    sanitized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))
    reparsed = urlsplit(sanitized)
    if reparsed.username or reparsed.password or reparsed.query or reparsed.fragment:
        raise GitLabProtocolError("repository URL could not be sanitized")
    return sanitized
