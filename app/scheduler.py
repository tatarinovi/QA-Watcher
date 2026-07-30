from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.deployment_ingestor import PollResult

logger = logging.getLogger(__name__)


class Poller(Protocol):
    def poll(self) -> PollResult: ...


class DeploymentNotifier(Protocol):
    def deployment_detected(
        self,
        *,
        repository: str,
        environment: str,
        version: str,
        description: str,
    ) -> None: ...


class NotifyingPoller:
    """Notify only after a newly detected actionable event is durable."""

    def __init__(
        self, poller: Poller, notifier: DeploymentNotifier
    ) -> None:
        self._poller = poller
        self._notifier = notifier

    def poll(self) -> PollResult:
        result = self._poller.poll()
        for notification in result.notifications:
            try:
                self._notifier.deployment_detected(
                    repository=notification.repository,
                    environment=notification.environment,
                    version=notification.version,
                    description=notification.description,
                )
            except Exception as exc:  # noqa: BLE001 - notification must not break ingestion
                logger.error("deployment notification failed: %s", type(exc).__name__)
        return result


class MultiProjectPoller:
    def __init__(self, pollers: list[Poller]) -> None:
        if not pollers:
            raise ValueError("at least one project poller is required")
        self._pollers = tuple(pollers)

    @property
    def pollers_count(self) -> int:
        return len(self._pollers)

    def poll(self) -> PollResult:
        results: list[PollResult] = []
        for poller in self._pollers:
            try:
                results.append(poller.poll())
            except Exception as exc:  # noqa: BLE001 - isolate allowlisted projects
                logger.error("project poll failed: %s", type(exc).__name__)
                results.append(
                    PollResult(
                        added=0,
                        rejected=1,
                        rejection_reasons=("project_poll_failed",),
                    )
                )
        return PollResult(
            added=sum(result.added for result in results),
            rejected=sum(result.rejected for result in results),
            rejection_reasons=tuple(
                reason for result in results for reason in result.rejection_reasons
            ),
            notifications=tuple(
                notification
                for result in results
                for notification in result.notifications
            ),
        )


class PollScheduler:
    def __init__(self, poller: Poller, interval_seconds: int) -> None:
        self._poller = poller
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self._poll,
            "interval",
            seconds=interval_seconds,
            id="gitlab-deployment-poll",
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _poll(self) -> None:
        try:
            result = self._poller.poll()
        except Exception as exc:  # noqa: BLE001 - scheduler jobs must not terminate the process
            logger.error("deployment poll failed: %s", type(exc).__name__)
            return
        logger.info(
            "deployment poll completed added=%d rejected=%d",
            result.added,
            result.rejected,
        )
