# tests/conftest.py
import pytest
from dataclasses import dataclass, field
from google.transit.gtfs_realtime_pb2 import FeedMessage


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


@pytest.fixture
def make_response():
    """Factory fixture — call it inside a test to build a FakeResponse."""

    def _make(status_code=200, headers=None, content=b""):
        return _FakeResponse(
            status_code=status_code,
            headers=headers or {},
            content=content,
        )

    return _make


@pytest.fixture
def valid_protobuf_bytes():
    msg = FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    return msg.SerializeToString()
