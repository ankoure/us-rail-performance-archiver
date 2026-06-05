import json

from archiver.archiver import FeedArchiver
from archiver.decoder import Decoder
from archiver.feed import Feed
from archiver.parser import Parser
from archiver.poll_state import PollStateStore
from archiver.writer import LocalWriter
from tests.fakes.client import _ConditionalFakeClient, _FakeClient, _SequenceFakeClient


async def test_duplicate_poll_writes_no_bin(
    make_response, valid_protobuf_bytes, tmp_path
):
    feed = Feed(
        name="test-feed",
        path="/x",
        client=_FakeClient(make_response(content=valid_protobuf_bytes)),
        parser=Parser.from_name("protobuf"),
        decoder=Decoder.from_name("standard"),
    )
    writer = LocalWriter(str(tmp_path))
    archiver = FeedArchiver(feeds=[feed], writer=writer, store=PollStateStore())

    await archiver.archive_one(feed)  # first poll  -> stores bytes
    await archiver.archive_one(feed)  # second poll -> identical -> DuplicateResponse

    bins = list(tmp_path.rglob("*.bin"))
    assert len(bins) == 1, "duplicate poll should not write a second .bin"

    metadata = list(tmp_path.rglob("*.jsonl"))
    with open(metadata[0]) as f:
        records = [json.loads(line) for line in f]
        assert len(records) == 2, "both polls should write metadata"
        assert records[0]["timestamp"] <= records[1]["timestamp"], (
            "timestamps should increase"
        )
        assert records[0]["digest"] == records[1]["digest"], "digests should match"
        assert records[0]["status_code"] == 200, "first poll should be 200"
        assert records[1]["status_code"] == 200, "second poll should also be 200"
        assert records[0]["response_type"] == "ProtobufResponse", (
            "first poll should indicate ProtobufResponse"
        )
        assert records[1]["response_type"] == "DuplicateResponse", (
            "second poll should indicate DuplicateResponse"
        )


async def test_304_stores_nothing(valid_protobuf_bytes, tmp_path):
    feed = Feed(
        name="test-feed",
        path="/x",
        client=_ConditionalFakeClient(
            content=valid_protobuf_bytes,
            etag="v1",
        ),
        parser=Parser.from_name("protobuf"),
        decoder=Decoder.from_name("standard"),
    )
    writer = LocalWriter(str(tmp_path))
    archiver = FeedArchiver(feeds=[feed], writer=writer, store=PollStateStore())
    await archiver.archive_one(feed)  # first poll  -> stores bytes
    await archiver.archive_one(feed)  # second poll -> doesn't

    bins = list(tmp_path.rglob("*.bin"))
    assert len(bins) == 1, "duplicate poll should not write a second .bin"

    metadata = list(tmp_path.rglob("*.jsonl"))
    with open(metadata[0]) as f:
        records = [json.loads(line) for line in f]
        assert len(records) == 2, "both polls should write metadata"
        assert records[0]["digest"] == records[1]["digest"], "digests should match"
        assert records[0]["status_code"] == 200, "first poll should be 200"
        assert records[1]["status_code"] == 304, "second poll should be 304"
        assert records[0]["response_type"] == "ProtobufResponse", (
            "first poll should indicate ProtobufResponse"
        )
        assert records[1]["response_type"] == "NotModifiedResponse", (
            "second poll should indicate NotModifiedResponse"
        )
        assert feed.client.calls[1]["If-None-Match"] == "v1"


async def test_archiver_respects_last_modified(valid_protobuf_bytes, tmp_path):
    feed = Feed(
        name="test-feed",
        path="/x",
        client=_ConditionalFakeClient(
            content=valid_protobuf_bytes,
            last_modified="Sun, 02 Jun 2026 12:00:00 GMT",
        ),
        parser=Parser.from_name("protobuf"),
        decoder=Decoder.from_name("standard"),
    )
    writer = LocalWriter(str(tmp_path))
    archiver = FeedArchiver(feeds=[feed], writer=writer, store=PollStateStore())
    await archiver.archive_one(feed)  # first poll  -> stores bytes
    await archiver.archive_one(feed)  # second poll -> doesn't
    bins = list(tmp_path.rglob("*.bin"))
    assert len(bins) == 1, "duplicate poll should not write a second .bin"

    metadata = list(tmp_path.rglob("*.jsonl"))
    with open(metadata[0]) as f:
        records = [json.loads(line) for line in f]
        assert len(records) == 2, "both polls should write metadata"
        assert records[0]["digest"] == records[1]["digest"], "digests should match"
        assert records[0]["status_code"] == 200, "first poll should be 200"
        assert records[1]["status_code"] == 304, "second poll should be 304"
        assert records[0]["response_type"] == "ProtobufResponse", (
            "first poll should indicate ProtobufResponse"
        )
        assert records[1]["response_type"] == "NotModifiedResponse", (
            "second poll should indicate NotModifiedResponse"
        )
        assert (
            feed.client.calls[1]["If-Modified-Since"] == "Sun, 02 Jun 2026 12:00:00 GMT"
        )


async def test_transient_error_does_not_update_poll_state(
    make_response, valid_protobuf_bytes, tmp_path
):
    feed = Feed(
        name="test-feed",
        path="/x",
        client=_SequenceFakeClient(
            [
                make_response(
                    content=valid_protobuf_bytes
                ),  # first poll -> stores bytes
                make_response(status_code=500),  # second poll -> transient error
                make_response(
                    content=valid_protobuf_bytes
                ),  # third poll -> identical -> DuplicateResponse
            ]
        ),
        parser=Parser.from_name("protobuf"),
        decoder=Decoder.from_name("standard"),
    )
    writer = LocalWriter(str(tmp_path))
    archiver = FeedArchiver(feeds=[feed], writer=writer, store=PollStateStore())
    await archiver.archive_one(feed)  # first poll  -> stores bytes
    await archiver.archive_one(
        feed
    )  # second poll -> transient error, should not update state
    await archiver.archive_one(
        feed
    )  # third poll -> identical to first, should be DuplicateResponse

    bins = list(tmp_path.rglob("*.bin"))
    assert len(bins) == 1, "only the first poll should write a .bin"

    metadata = list(tmp_path.rglob("*.jsonl"))
    with open(metadata[0]) as f:
        records = [json.loads(line) for line in f]
        assert len(records) == 3, "all three polls should write metadata"
        assert records[0]["digest"] == records[2]["digest"], (
            "first and third digests should match"
        )
        assert records[1]["status_code"] == 500, (
            "second poll should be a transient error"
        )
        assert records[2]["response_type"] == "DuplicateResponse", (
            "third poll should indicate DuplicateResponse"
        )
