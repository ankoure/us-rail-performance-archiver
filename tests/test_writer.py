from datetime import datetime, timezone
import hashlib
import io
import json
from dataclasses import dataclass
from archiver.writer import (
    BadHeaderError,
    BatchingWriter,
    CorruptFrameError,
    FrameReader,
    FrameWriter,
    LocalWriter,
)
import pytest


@dataclass
class FakeWriteableResponse:
    """Minimal fake — only what LocalWriter actually reads."""

    _timestamp = datetime(2026, 5, 4, tzinfo=timezone.utc).timestamp()
    _payload: bytes
    _metadata: dict

    def raw_payload(self) -> bytes:
        return self._payload

    def to_metadata_row(self) -> dict:
        return self._metadata

    def get_datetime(self) -> datetime:
        return datetime.fromtimestamp(self._timestamp, tz=timezone.utc)

    def get_timestamp(self) -> float:
        return self._timestamp

    def content_digest(self) -> str:
        return hashlib.sha256(self._payload).hexdigest()


@dataclass
class FakeEmptyPayloadResponse(FakeWriteableResponse):
    def raw_payload(self) -> bytes:
        return None


def test_writer_creates_raw_file_and_metadata(tmp_path):
    writer = LocalWriter(base_dir=str(tmp_path))
    response = FakeWriteableResponse(
        _payload=b"\x01\x02\x03",
        _metadata={"status_code": 200, "response_type": "ProtobufResponse"},
    )

    writer.write(feed_name="test-feed", response=response)
    date = response.get_datetime()

    # raw file landed under the expected path
    raw_files = list(
        (
            tmp_path
            / "test-feed"
            / "raw"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
        ).iterdir()
    )
    assert len(raw_files) == 1
    assert raw_files[0].suffix == ".bin"
    assert raw_files[0].read_bytes() == b"\x01\x02\x03"

    # metadata file has exactly one line, matching what we passed in
    metadata_lines = (
        (
            tmp_path
            / "test-feed"
            / "metadata"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
            / "data.jsonl"
        )
        .read_text()
        .splitlines()
    )
    assert len(metadata_lines) == 1
    row = json.loads(metadata_lines[0])
    assert row == {"status_code": 200, "response_type": "ProtobufResponse"}


def test_errorresponse_writes_no_bin(tmp_path):
    writer = LocalWriter(base_dir=str(tmp_path))
    response = FakeEmptyPayloadResponse(
        _payload=b"",
        _metadata={"status_code": 401, "response_type": "ErrorResponse"},
    )

    writer.write(feed_name="test-feed", response=response)
    date = response.get_datetime()

    # raw file landed under the expected path
    raw_files = list(
        (
            tmp_path
            / "test-feed"
            / "raw"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
        ).iterdir()
    )
    assert len(raw_files) == 0

    # metadata file has exactly one line, matching what we passed in
    metadata_lines = (
        (
            tmp_path
            / "test-feed"
            / "metadata"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
            / "data.jsonl"
        )
        .read_text()
        .splitlines()
    )
    assert len(metadata_lines) == 1
    row = json.loads(metadata_lines[0])
    assert row == {"status_code": 401, "response_type": "ErrorResponse"}


def test_round_trip():
    # --- write ---
    payloads = [
        b"hello world",
        b"second frame",
        b"\x00\x01\x02\x03 binary data",
    ]

    buf = io.BytesIO()
    writer = FrameWriter(buf)
    for payload in payloads:
        digest = hashlib.sha256(payload).digest()
        writer.write_frame(payload, digest)

    # --- read ---
    buf.seek(0)
    frames = list(FrameReader(buf))

    assert len(frames) == 3
    for (payload, digest), expected in zip(frames, payloads):
        assert payload == expected
        assert digest == hashlib.sha256(expected).digest()

    print("round trip ok")


def test_corrupt_frame():
    buf = io.BytesIO()
    writer = FrameWriter(buf)
    payload = b"hello world"
    digest = hashlib.sha256(payload).digest()
    writer.write_frame(payload, digest)

    # flip a byte in the payload
    raw = bytearray(buf.getvalue())
    raw[-1] ^= 0xFF
    buf = io.BytesIO(bytes(raw))

    with pytest.raises(CorruptFrameError):
        list(FrameReader(buf))


def test_bad_magic():
    buf = io.BytesIO(b"\x00\x00\x00\x00\x00")
    with pytest.raises(BadHeaderError):
        list(FrameReader(buf))


def test_batching_writer_flushes_closed_window(tmp_path):
    writer = BatchingWriter(base_dir=str(tmp_path), window_seconds=300)

    t1 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 4, 12, 1, 0, tzinfo=timezone.utc)

    r1 = FakeWriteableResponse(_payload=b"payload one", _metadata={"status_code": 200})
    r2 = FakeWriteableResponse(_payload=b"payload two", _metadata={"status_code": 200})

    # override timestamps so both land in the same window
    r1._timestamp = t1.timestamp()
    r2._timestamp = t2.timestamp()

    writer.write(feed_name="test-feed", response=r1)
    writer.write(feed_name="test-feed", response=r2)

    # flush from inside the window — nothing should be written yet
    writer.flush_due(t2.timestamp())
    raw_files = list((tmp_path / "test-feed" / "raw").rglob("*.bin"))
    assert len(raw_files) == 0

    # flush from past the window boundary — one object with two frames
    now = datetime(2026, 5, 4, 12, 10, 0, tzinfo=timezone.utc)
    writer.flush_due(now.timestamp())

    raw_files = list((tmp_path / "test-feed" / "raw").rglob("*.bin"))
    assert len(raw_files) == 1

    with open(raw_files[0], "rb") as fh:
        frames = list(FrameReader(fh))
    assert len(frames) == 2
    payloads = {f[0] for f in frames}
    assert payloads == {b"payload one", b"payload two"}


def test_batching_writer_does_not_flush_open_window(tmp_path):
    writer = BatchingWriter(base_dir=str(tmp_path), window_seconds=300)

    t1 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    r1 = FakeWriteableResponse(_payload=b"payload one", _metadata={"status_code": 200})
    r1._timestamp = t1.timestamp()

    writer.write(feed_name="test-feed", response=r1)

    # flush from inside the same window — nothing should be written
    now = datetime(2026, 5, 4, 12, 4, 0, tzinfo=timezone.utc)
    writer.flush_due(now.timestamp())

    raw_files = list((tmp_path / "test-feed" / "raw").rglob("*.bin"))
    assert len(raw_files) == 0
    assert len(writer._buffer) == 1
