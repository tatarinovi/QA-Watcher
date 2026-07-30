from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from app.models import (
    CompletionReason,
    DeploymentEvent,
    EnvironmentState,
    EventStatus,
    ProjectSnapshot,
    State,
)
from app.storage import (
    ConcurrentStateUpdateError,
    CorruptStateError,
    EventStore,
    ImmutableDeploymentError,
    JsonEventStore,
    SchemaVersionError,
)

NOW = datetime(2026, 7, 29, 17, 30, tzinfo=UTC)


def event(
    deployment_id: int,
    *,
    environment: str = "test",
    sha: str | None = None,
    previous_sha: str | None = "a" * 40,
    status: EventStatus = EventStatus.PENDING,
    completion_reason: CompletionReason | None = None,
) -> DeploymentEvent:
    return DeploymentEvent(
        project_id=42,
        deployment_id=deployment_id,
        environment=environment,
        sha=sha or f"{deployment_id:040x}",
        previous_sha=previous_sha,
        finished_at=NOW,
        discovered_at=NOW,
        status=status,
        pipeline_ref="test",
        completion_reason=completion_reason,
    )


def test_models_round_trip_and_event_identity_is_immutable() -> None:
    original = State(
        environments={
            "42:test": EnvironmentState(
                last_seen_deployment_id=12,
                last_seen_sha="b" * 40,
                last_completed_deployment_id=11,
                last_completed_sha="a" * 40,
                updated_at=NOW,
            )
        },
        deployments={
            "42:11": event(11, sha="a" * 40, status=EventStatus.COMPLETED),
            "42:12": event(12, sha="b" * 40),
        },
    )

    restored = State.model_validate_json(original.model_dump_json())

    assert restored == original
    with pytest.raises(ValidationError):
        restored.deployments["42:12"].deployment_id = 13


def test_v2_state_migrates_composite_keys_and_preserves_legacy_report_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    legacy_event = event(77).model_copy(
        update={
            "project": ProjectSnapshot(
                gitlab_host="gitlab.example",
                project_id=42,
                name="app",
                web_url="https://gitlab.example/app",
                http_repo_url="https://gitlab.example/app.git",
                ssh_repo_url="ssh://gitlab.example/app.git",
            )
        }
    )
    raw_event = legacy_event.model_dump(mode="json")
    raw_event.pop("project_id")
    raw_event.pop("pipeline_ref")
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 5,
                "environments": {
                    "test": {
                        "last_seen_deployment_id": 77,
                        "last_seen_sha": legacy_event.sha,
                        "last_completed_deployment_id": None,
                        "last_completed_sha": None,
                        "updated_at": None,
                    }
                },
                "deployments": {"77": raw_event},
            }
        ),
        encoding="utf-8",
    )

    migrated = JsonEventStore(path).load()

    assert set(migrated.deployments) == {"42:77"}
    assert set(migrated.environments) == {"42:test"}
    assert migrated.deployments["42:77"].pipeline_ref is None
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3


def test_v2_migration_fails_closed_without_project_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    raw_event = event(77).model_dump(mode="json")
    raw_event.pop("project_id")
    raw_event.pop("pipeline_ref")
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "environments": {},
                "deployments": {"77": raw_event},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorruptStateError, match="project snapshot"):
        JsonEventStore(path).load()


def test_v2_migration_fails_closed_for_mismatched_deployment_key(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    legacy_event = event(77).model_copy(
        update={
            "project": ProjectSnapshot(
                gitlab_host="gitlab.example",
                project_id=42,
                name="app",
                web_url="https://gitlab.example/app",
                http_repo_url="https://gitlab.example/app.git",
                ssh_repo_url="ssh://gitlab.example/app.git",
            )
        }
    )
    raw_event = legacy_event.model_dump(mode="json")
    raw_event.pop("project_id")
    raw_event.pop("pipeline_ref")
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 0,
                "environments": {},
                "deployments": {"76": raw_event},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorruptStateError, match="does not match deployment ID"):
        JsonEventStore(path).load()


def test_models_reject_inconsistent_keys_and_terminal_dispositions() -> None:
    with pytest.raises(ValidationError, match="deployment key"):
        State(deployments={"42:99": event(12)})
    with pytest.raises(ValidationError, match="initial_baseline"):
        event(
            1,
            previous_sha=None,
            status=EventStatus.PENDING,
            completion_reason=CompletionReason.INITIAL_BASELINE,
        )
    with pytest.raises(ValidationError, match="no_changes"):
        event(
            2,
            sha="a" * 40,
            previous_sha="b" * 40,
            status=EventStatus.COMPLETED,
            completion_reason=CompletionReason.NO_CHANGES,
        )


def test_independent_environment_cursors_and_chronological_queue() -> None:
    state = State(
        environments={
            "42:test": EnvironmentState(last_seen_deployment_id=3, last_seen_sha="3" * 40),
            "42:stage": EnvironmentState(last_seen_deployment_id=8, last_seen_sha="8" * 40),
        },
        deployments={
            "42:3": event(3, environment="test", sha="3" * 40),
            "42:8": event(8, environment="stage", sha="8" * 40),
            "42:2": event(2, environment="test"),
        },
    )

    assert state.environments["42:test"].last_seen_deployment_id == 3
    assert state.environments["42:stage"].last_seen_deployment_id == 8
    assert [item.deployment_id for item in state.ordered_events()] == [2, 3, 8]


@pytest.mark.parametrize(
    ("previous_sha", "sha", "reason"),
    [
        (None, "a" * 40, CompletionReason.INITIAL_BASELINE),
        ("a" * 40, "a" * 40, CompletionReason.NO_CHANGES),
    ],
)
def test_baseline_and_no_changes_are_terminal(
    previous_sha: str | None,
    sha: str,
    reason: CompletionReason,
) -> None:
    terminal = event(
        10,
        previous_sha=previous_sha,
        sha=sha,
        status=EventStatus.COMPLETED,
        completion_reason=reason,
    )

    assert terminal.status is EventStatus.COMPLETED
    assert terminal.completion_reason is reason


@pytest.mark.parametrize("environment", ["/review", "review/../prod", "qa", ""])
def test_state_rejects_noncanonical_environment_names(environment: str) -> None:
    with pytest.raises(ValidationError):
        State(deployments={"42:1": event(1, environment=environment)})


def test_status_values_cover_the_phase_1_state_machine() -> None:
    assert [status.value for status in EventStatus] == ["pending", "claimed", "completed"]


def test_json_store_implements_protocol_and_persists_state(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = JsonEventStore(path)
    assert isinstance(store, EventStore)

    first = store.load()
    assert first == State()
    saved = store.save(State(deployments={"42:12": event(12)}), expected_revision=0)

    assert saved.revision == 1
    assert JsonEventStore(path).load() == saved
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3


def test_json_store_rejects_stale_revision(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    store.save(State(deployments={"42:1": event(1)}), expected_revision=0)

    with pytest.raises(ConcurrentStateUpdateError):
        store.save(State(deployments={"42:2": event(2)}), expected_revision=0)


def test_json_store_rejects_changes_to_existing_deployment_identity(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    original = store.save(State(deployments={"42:1": event(1)}), expected_revision=0)
    changed_identity = event(1, environment="stage")

    with pytest.raises(ImmutableDeploymentError):
        store.save(
            State(revision=original.revision, deployments={"42:1": changed_identity}),
            expected_revision=original.revision,
        )

    assert store.load() == original


def test_json_store_revalidates_mutated_state_instance_before_write(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    saved = store.save(State(deployments={"42:1": event(1)}), expected_revision=0)
    saved.deployments["wrong-key"] = event(2)

    with pytest.raises(ValidationError, match="deployment key"):
        store.save(saved, expected_revision=saved.revision)


def test_json_store_rejects_removal_of_durable_deployment(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    saved = store.save(State(deployments={"42:1": event(1)}), expected_revision=0)

    with pytest.raises(ImmutableDeploymentError, match="cannot be removed"):
        store.save(State(revision=saved.revision), expected_revision=saved.revision)

    assert store.load() == saved


def test_json_store_rejects_completed_event_regression(tmp_path: Path) -> None:
    store = JsonEventStore(tmp_path / "state.json")
    completed = event(1, status=EventStatus.COMPLETED)
    saved = store.save(State(deployments={"42:1": completed}), expected_revision=0)

    with pytest.raises(ImmutableDeploymentError, match="terminal"):
        store.save(
            State(revision=saved.revision, deployments={"42:1": event(1)}),
            expected_revision=saved.revision,
        )

    assert store.load() == saved


def test_environment_cursors_must_match_referenced_deployments() -> None:
    completed = event(1, environment="test", status=EventStatus.COMPLETED)
    pending = event(2, environment="test")

    with pytest.raises(ValidationError, match="last_seen cursor environment"):
        State(
            environments={
                "42:stage": EnvironmentState(last_seen_deployment_id=1, last_seen_sha=completed.sha)
            },
            deployments={"42:1": completed},
        )
    with pytest.raises(ValidationError, match="last_seen cursor SHA"):
        State(
            environments={
                "42:test": EnvironmentState(last_seen_deployment_id=1, last_seen_sha="f" * 40)
            },
            deployments={"42:1": completed},
        )
    with pytest.raises(ValidationError, match="last_completed cursor requires completed"):
        State(
            environments={
                "42:test": EnvironmentState(
                    last_completed_deployment_id=2,
                    last_completed_sha=pending.sha,
                )
            },
            deployments={"42:2": pending},
        )


def test_all_processes_use_the_same_sidecar_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = JsonEventStore(path)
    script = (
        "from pathlib import Path\n"
        "from filelock import Timeout\n"
        "from app.storage import JsonEventStore\n"
        f"store = JsonEventStore(Path({str(path)!r}), lock_timeout=0.05)\n"
        "try:\n"
        "    store.load()\n"
        "except Timeout:\n"
        "    raise SystemExit(23)\n"
    )

    with store._lock:
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            cwd=Path(__file__).parents[1],
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        )

    assert result.returncode == 23


@pytest.mark.parametrize("contents", ["{", "null", "[]", '{"schema_version": 2} trailing'])
def test_json_store_fails_closed_for_corrupt_json(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(CorruptStateError):
        JsonEventStore(path).load()


@pytest.mark.parametrize("version", [1, 4, "2"])
def test_json_store_fails_closed_for_schema_mismatch(tmp_path: Path, version: object) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": version,
                "revision": 0,
                "environments": {},
                "deployments": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SchemaVersionError):
        JsonEventStore(path).load()


def test_failed_validation_does_not_replace_existing_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = JsonEventStore(path)
    saved = store.save(State(deployments={"42:1": event(1)}), expected_revision=0)
    invalid = saved.model_dump(mode="python")
    invalid["deployments"]["42:1"]["deployment_id"] = 999

    with pytest.raises(ValidationError):
        store.save(cast(State, invalid), expected_revision=saved.revision)

    assert store.load() == saved


def test_atomic_write_fsyncs_file_replaces_then_fsyncs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    calls: list[tuple[str, Path | int]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracking_fsync(fd: int) -> None:
        calls.append(("fsync", fd))
        real_fsync(fd)

    def tracking_replace(source: str | Path, target: str | Path) -> None:
        calls.append(("replace", Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "replace", tracking_replace)

    JsonEventStore(path).save(State(deployments={"42:1": event(1)}), expected_revision=0)

    replace_index = next(i for i, call in enumerate(calls) if call[0] == "replace")
    assert any(call[0] == "fsync" for call in calls[:replace_index])
    assert any(call[0] == "fsync" for call in calls[replace_index + 1 :])
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))
