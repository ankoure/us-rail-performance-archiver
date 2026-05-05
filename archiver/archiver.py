from archiver.response import TransportErrorResponse, parse_response
from archiver.feed import Feed
from archiver.writer import LocalWriter
import requests
from archiver.logger import logger


class FeedArchiver:
    def __init__(self, feeds: list[Feed], writer: LocalWriter):
        self.feeds = feeds
        self.writer = writer

    def archive_once(self):
        for feed in self.feeds:
            try:
                response = parse_response(
                    feed.client.get(feed.path), feed.expected_format
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
