from archiver.response import TransportErrorResponse
from archiver.parser import parse_response
from archiver.feed import Feed
from archiver.writer import LocalWriter
import requests
from archiver.logger import logger
from archiver.telemetry import Telemetry, NoOpTelemetry


class FeedArchiver:
    def __init__(
        self, feeds: list[Feed], writer: LocalWriter, telemetry: Telemetry | None = None
    ):
        self.feeds = feeds
        self.writer = writer
        self.telemetry = telemetry or NoOpTelemetry()

    def archive_once(self):
        for feed in self.feeds:
            try:
                with self.telemetry.span(
                    "feed.poll", resource=feed.name, tags={"feed": feed.name}
                ):
                    response = parse_response(feed.client.get(feed.path), feed.parser)
            except requests.RequestException as e:
                response = TransportErrorResponse(
                    error_type=type(e).__name__, error_message=str(e)
                )
                logger.warning(
                    "Transport error on feed %s: %s: %s",
                    feed.name,
                    type(e).__name__,
                    e,
                )
            self.writer.write(feed.name, response)
