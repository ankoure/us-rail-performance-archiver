from concurrent.futures import ThreadPoolExecutor

from archiver.archiver import FeedArchiver
from archiver.logger import logger
from archiver.feed import Feed
from archiver.scheduler import Scheduler
from archiver.telemetry import NoOpTelemetry, Telemetry


class Dispatcher:
    def __init__(
        self,
        scheduler: Scheduler,
        archiver: FeedArchiver,
        executor: ThreadPoolExecutor,
        telemetry: Telemetry | None = None,
    ):
        self._scheduler = scheduler
        self._archiver = archiver
        self._pool = executor
        self.telemetry = telemetry or NoOpTelemetry()

    def submit(self, feed: Feed):
        self._scheduler.mark_polled(feed)
        self._pool.submit(self._safe_archive, feed)

    def _safe_archive(self, feed: Feed) -> None:
        try:
            self._archiver.archive_one(feed)

        except Exception as exc:
            self.telemetry.incr(
                "dispatch.error",
                tags={"feed": feed.name, "error_type": type(exc).__name__},
            )
            logger.exception("unexpected error archiving %s", feed.name)
