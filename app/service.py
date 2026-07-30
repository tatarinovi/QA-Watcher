from __future__ import annotations

from app.deployment_ingestor import DeploymentIngestor
from app.gitlab_client import GitLabClient
from app.pending_query import PendingDeployment, PendingQuery
from app.storage import EventStore


class QAWatcher:
    """Compatibility facade backed entirely by the durable EventStore."""

    def __init__(self, gitlab: GitLabClient, store: EventStore) -> None:
        self._ingestor = DeploymentIngestor(gitlab, store)
        self._query = PendingQuery(store)

    def poll(self) -> None:
        self._ingestor.poll()

    def list_pending(self, environment: str | None = None) -> list[PendingDeployment]:
        return list(
            self._query.list_pending(environment=environment, limit=100).items
        )
