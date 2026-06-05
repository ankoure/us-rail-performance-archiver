"""End-to-end check of the real async transport feeding M5's failure classifier.

Drives a real ``APIClient`` (httpx.AsyncClient) via ``httpx.MockTransport`` through
``FeedArchiver.archive_one`` and asserts the *returned* response classifies the way
the poll loop's ``_on_done`` relies on for backoff/quarantine. (A full ``run()``
loop test would need a dependency-injection seam into ``run``; the loop wiring
itself is covered by the manual end-to-end run + the unit tests.)
"""

import httpx

from archiver.archiver import FeedArchiver
from archiver.auth import APIClient
from archiver.decoder import Decoder
from archiver.feed import Feed
from archiver.health import is_transient_failure
from archiver.parser import Parser
from archiver.poll_state import PollStateStore
from archiver.response import ProtobufResponse, TransportErrorResponse
from archiver.writer import LocalWriter


def _client(handler) -> APIClient:
    c = APIClient("https://feed.test")
    c.client._transport = httpx.MockTransport(handler)
    return c


def _feed(client) -> Feed:
    return Feed(
        name="f",
        path="/x",
        client=client,
        parser=Parser.from_name("protobuf"),
        decoder=Decoder.from_name("standard"),
    )


def _archiver(feed, tmp_path) -> FeedArchiver:
    return FeedArchiver(
        feeds=[feed], writer=LocalWriter(str(tmp_path)), store=PollStateStore()
    )


async def test_success_returns_non_failure(tmp_path, valid_protobuf_bytes):
    def handler(request):
        return httpx.Response(200, content=valid_protobuf_bytes)

    feed = _feed(_client(handler))
    arch = _archiver(feed, tmp_path)
    async with feed.client:
        resp = await arch.archive_one(feed)
    assert isinstance(resp, ProtobufResponse)
    assert resp.status_code == 200
    assert not is_transient_failure(resp)


async def test_5xx_is_failure(tmp_path):
    def handler(request):
        return httpx.Response(500, content=b"")

    feed = _feed(_client(handler))
    arch = _archiver(feed, tmp_path)
    async with feed.client:
        resp = await arch.archive_one(feed)
    assert resp.status_code == 500
    assert is_transient_failure(resp)


async def test_transport_error_is_failure(tmp_path):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    feed = _feed(_client(handler))
    arch = _archiver(feed, tmp_path)
    async with feed.client:
        resp = await arch.archive_one(feed)
    assert isinstance(resp, TransportErrorResponse)
    assert is_transient_failure(resp)
