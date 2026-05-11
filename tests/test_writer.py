from datetime import datetime, timezone
import json
from dataclasses import dataclass
from archiver.writer import LocalWriter


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


@dataclass
class FakeErrorResponse(FakeWriteableResponse):
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
    response = FakeErrorResponse(
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
