import time

from archiver.response import DecodeFailureResponse, TransportErrorResponse
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

    def archive_one(self, feed: Feed):
        try:
            with self.telemetry.span("feed.poll", tags={"feed": feed.name}):
                start = time.monotonic()
                response = parse_response(
                    feed.client.get(feed.path), feed.parser, feed.decoder
                )
                poll_duration = time.monotonic() - start
            if isinstance(response, DecodeFailureResponse):
                self.telemetry.incr("decoder.schema_drift", tags={"feed": feed.name})

            if feed.poll_interval_seconds:
                if poll_duration > feed.poll_interval_seconds:
                    logger.warning(
                        "Poll for %s took %.2fs, exceeds configured interval %ds",
                        feed.name,
                        poll_duration,
                        feed.poll_interval_seconds,
                    )
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
