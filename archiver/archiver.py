import time

from archiver.poll_state import PollState, PollStateStore
from archiver.response import (
    DecodeFailureResponse,
    DuplicateResponse,
    JsonResponse,
    NotModifiedResponse,
    ProtobufResponse,
    TransportErrorResponse,
)
from archiver.parser import parse_response
from archiver.feed import Feed
from archiver.writer import LocalWriter
import httpx
from archiver.logger import logger
from archiver.telemetry import Telemetry, NoOpTelemetry


class FeedArchiver:
    def __init__(
        self,
        feeds: list[Feed],
        writer: LocalWriter,
        store: PollStateStore,
        telemetry: Telemetry | None = None,
    ):
        self.feeds = feeds
        self.writer = writer
        self.telemetry = telemetry or NoOpTelemetry()
        self.store = store

    async def archive_one(self, feed: Feed):
        try:
            prior = self.store.get(feed.name)
            conditional_headers = {}
            if prior.etag:
                conditional_headers["If-None-Match"] = prior.etag
            if prior.last_modified:
                conditional_headers["If-Modified-Since"] = prior.last_modified

            try:
                with self.telemetry.span("feed.poll", tags={"feed": feed.name}):
                    start = time.monotonic()
                    http = await feed.client.get(feed.path, headers=conditional_headers)

                    if http.status_code == 304:
                        self.telemetry.incr(
                            "feed.not_modified", tags={"feed": feed.name}
                        )
                        response = NotModifiedResponse(http, prior.last_digest)
                    else:
                        response = parse_response(http, feed.parser, feed.decoder)
                        current_digest = response.content_digest()
                        if current_digest == prior.last_digest:
                            self.telemetry.incr(
                                "feed.duplicate", tags={"feed": feed.name}
                            )
                            response = DuplicateResponse(http)
                        else:
                            if isinstance(response, (ProtobufResponse, JsonResponse)):
                                self.store.set(
                                    feed.name,
                                    PollState(
                                        etag=http.headers.get("ETag"),
                                        last_modified=http.headers.get("Last-Modified"),
                                        last_digest=current_digest,
                                    ),
                                )
                    poll_duration = time.monotonic() - start

                if isinstance(response, DecodeFailureResponse):
                    self.telemetry.incr(
                        "decoder.schema_drift", tags={"feed": feed.name}
                    )

                if feed.poll_interval_seconds:
                    if poll_duration > feed.poll_interval_seconds:
                        logger.warning(
                            "Poll for %s took %.2fs, exceeds configured interval %ds",
                            feed.name,
                            poll_duration,
                            feed.poll_interval_seconds,
                        )
            except httpx.RequestError as e:
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
        except Exception:
            self.telemetry.incr("poll.error", tags={"feed": feed.name})
            logger.exception("unexpected error polling %s", feed.name)
