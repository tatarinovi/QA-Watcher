from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from filelock import FileLock
from pydantic import ValidationError

from app.models import (
    SCHEMA_VERSION,
    EventStatus,
    State,
    deployment_key,
    environment_partition_key,
)


class StateStoreError(RuntimeError):
    """Base class for durable state failures."""


class CorruptStateError(StateStoreError):
    """The state file exists but is not valid durable state."""


class SchemaVersionError(StateStoreError):
    """The state belongs to an unsupported schema version."""


class ConcurrentStateUpdateError(StateStoreError):
    """The supplied snapshot is stale."""


class ImmutableDeploymentError(StateStoreError):
    """An existing deployment's immutable facts were changed."""


@runtime_checkable
class EventStore(Protocol):
    """Persistence boundary that can later be implemented with PostgreSQL."""

    def load(self) -> State: ...

    def save(self, state: State, *, expected_revision: int) -> State: ...


class JsonEventStore:
    """Single-replica JSON EventStore using one global sidecar-file lock."""

    def __init__(self, path: Path | str, *, lock_timeout: float = 30.0) -> None:
        self._path = Path(path)
        self._lock = FileLock(f"{self._path}.lock", timeout=lock_timeout)

    def load(self) -> State:
        with self._lock:
            return self._load_unlocked()

    def save(self, state: State, *, expected_revision: int) -> State:
        validated = State.model_validate(state)
        with self._lock:
            current = self._load_unlocked()
            if current.revision != expected_revision:
                raise ConcurrentStateUpdateError(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            for deployment_id, existing in current.deployments.items():
                replacement = validated.deployments.get(deployment_id)
                if replacement is None:
                    raise ImmutableDeploymentError(
                        f"deployment {deployment_id} cannot be removed"
                    )
                if replacement.identity != existing.identity:
                    raise ImmutableDeploymentError(
                        f"deployment {deployment_id} identity cannot be changed"
                    )
                if (
                    existing.status is EventStatus.COMPLETED
                    and replacement.status is not EventStatus.COMPLETED
                ):
                    raise ImmutableDeploymentError(
                        f"deployment {deployment_id} has terminal completed status"
                    )
                if existing.status is EventStatus.COMPLETED and replacement != existing:
                    raise ImmutableDeploymentError(
                        f"deployment {deployment_id} completed receipt is immutable"
                    )
                if (
                    existing.completion_intent is not None
                    and replacement.status is EventStatus.CLAIMED
                    and replacement.completion_intent != existing.completion_intent
                ):
                    raise ImmutableDeploymentError(
                        f"deployment {deployment_id} completion intent is immutable"
                    )
            saved = validated.model_copy(update={"revision": current.revision + 1})
            self._write_unlocked(saved)
            return saved

    def _load_unlocked(self) -> State:
        if not self._path.exists():
            return State()
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptStateError(f"cannot read valid JSON state: {self._path}") from exc
        if not isinstance(raw, dict):
            raise CorruptStateError("state root must be a JSON object")
        version = raw.get("schema_version")
        if version == 2 and not isinstance(version, bool):
            raw = _migrate_v2(raw)
            try:
                migrated = State.model_validate(raw)
            except ValidationError as exc:
                raise CorruptStateError("migrated state does not satisfy schema v3") from exc
            self._write_unlocked(migrated)
            return migrated
        if version != SCHEMA_VERSION or isinstance(version, bool):
            raise SchemaVersionError(
                f"expected schema_version {SCHEMA_VERSION}, "
                f"found {version!r}"
            )
        try:
            return State.model_validate(raw)
        except ValidationError as exc:
            raise CorruptStateError("state JSON does not satisfy schema v3") from exc

    def _write_unlocked(self, state: State) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(state.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
            self._fsync_directory()
        finally:
            temporary_path.unlink(missing_ok=True)

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self._path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _migrate_v2(raw: dict[str, Any]) -> dict[str, Any]:
    deployments = raw.get("deployments")
    environments = raw.get("environments")
    if not isinstance(deployments, dict) or not isinstance(environments, dict):
        raise CorruptStateError("schema v2 collections must be JSON objects")
    migrated_deployments: dict[str, Any] = {}
    legacy_ids: dict[int, list[int]] = {}
    for legacy_key, value in deployments.items():
        if not isinstance(value, dict):
            raise CorruptStateError("schema v2 deployment must be an object")
        project = value.get("project")
        project_id = project.get("project_id") if isinstance(project, dict) else None
        deployment_id = value.get("deployment_id")
        if (
            isinstance(project_id, bool)
            or not isinstance(project_id, int)
            or project_id <= 0
            or isinstance(deployment_id, bool)
            or not isinstance(deployment_id, int)
            or deployment_id <= 0
        ):
            raise CorruptStateError(
                f"schema v2 deployment {legacy_key!r} has no authoritative project snapshot"
            )
        if legacy_key != str(deployment_id):
            raise CorruptStateError(
                f"schema v2 deployment key {legacy_key!r} does not match deployment ID"
            )
        key = deployment_key(project_id, deployment_id)
        if key in migrated_deployments:
            raise CorruptStateError(f"schema v2 deployment identity {key} is ambiguous")
        migrated = dict(value)
        migrated["project_id"] = project_id
        # Schema v2 did not retain the exact GitLab pipeline ref. Do not fabricate
        # a value from the deployment environment: test and testing are distinct
        # factual fields. Newly ingested v3 records always retain the exact ref.
        migrated["pipeline_ref"] = migrated.get("pipeline_ref")
        migrated_deployments[key] = migrated
        legacy_ids.setdefault(deployment_id, []).append(project_id)

    migrated_environments: dict[str, Any] = {}
    for legacy_key, value in environments.items():
        if not isinstance(value, dict):
            raise CorruptStateError("schema v2 environment cursor must be an object")
        if ":" in legacy_key:
            project_raw, _, environment = legacy_key.partition(":")
            if not project_raw.isdigit() or int(project_raw) <= 0:
                raise CorruptStateError("invalid schema v2 environment partition")
            project_id = int(project_raw)
        else:
            environment = legacy_key
            cursor_ids = [
                value.get("last_seen_deployment_id"),
                value.get("last_completed_deployment_id"),
            ]
            candidates: set[int] = set()
            for deployment_id in cursor_ids:
                if deployment_id is None:
                    continue
                candidates.update(legacy_ids.get(deployment_id, ()))
            if len(candidates) != 1:
                raise CorruptStateError(
                    f"cannot safely bind schema v2 environment {legacy_key!r} to a project"
                )
            project_id = candidates.pop()
        key = environment_partition_key(project_id, environment)
        if key in migrated_environments:
            raise CorruptStateError(f"schema v2 environment identity {key} is ambiguous")
        migrated_environments[key] = value
    return {
        **raw,
        "schema_version": SCHEMA_VERSION,
        "deployments": migrated_deployments,
        "environments": migrated_environments,
    }
