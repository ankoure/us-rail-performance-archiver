import json
from dataclasses import dataclass
from archiver.writer import LocalWriter


@dataclass
class FakeWriteableResponse:
    """Minimal fake — only what LocalWriter actually reads."""

    _payload: bytes
    _metadata: dict

    def raw_payload(self) -> bytes:
        return self._payload

    def to_metadata_row(self) -> dict:
        return self._metadata


def test_writer_creates_raw_file_and_metadata(tmp_path):
    writer = LocalWriter(base_dir=str(tmp_path))
    response = FakeWriteableResponse(
        _payload=b"\x01\x02\x03",
        _metadata={"status_code": 200, "response_type": "ProtobufResponse"},
    )

    writer.write(feed_name="test-feed", response=response)

    # raw file landed under the expected path
    raw_files = list((tmp_path / "test-feed" / "raw").iterdir())
    assert len(raw_files) == 1
    assert raw_files[0].suffix == ".bin"
    assert raw_files[0].read_bytes() == b"\x01\x02\x03"

    # metadata file has exactly one line, matching what we passed in
    metadata_lines = (
        (tmp_path / "test-feed" / "metadata.jsonl").read_text().splitlines()
    )
    assert len(metadata_lines) == 1
    row = json.loads(metadata_lines[0])
    assert row == {"status_code": 200, "response_type": "ProtobufResponse"}
