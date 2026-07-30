from __future__ import annotations

from pathlib import Path

import httpx

from app.gitlab_client import GitLabClient
from app.service import QAWatcher
from app.storage import JsonEventStore


def test_http_gitlab_320_321_322_are_separate_fifo_events(
    fake_gitlab_client: httpx.Client,
    tmp_path: Path,
) -> None:
    watcher = QAWatcher(
        GitLabClient(fake_gitlab_client, project_id=42),
        JsonEventStore(tmp_path / "state.json"),
    )

    watcher.poll()
    items = watcher.list_pending("test")

    assert [(item.previous_sha[:3], item.sha[:3]) for item in items] == [
        ("320", "321"),
        ("321", "322"),
    ]
    assert [item.claimable for item in items] == [True, False]
    assert items[1].blocked_by_deployment_id == 321
    assert all(item.previous_sha[:3] != "320" or item.sha[:3] != "322" for item in items)
