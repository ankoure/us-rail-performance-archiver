import pytest

from archiver.sink import S3Sink, TeeSink
from tests.fakes.sink import FakeSink


class FakeUploader:
    """Records put_bytes calls; satisfies Uploader structurally (no inheritance)."""

    def __init__(self):
        self.calls = []  # list of (bucket, key, data)

    def put_bytes(self, bucket, key, data, *, storage_class=None, content_type=None):
        self.calls.append((bucket, key, data))


class ExplodingSink:
    """A sink whose put always fails — stands in for an S3 outage."""

    def put(self, key, data):
        raise RuntimeError("s3 is down")


def test_s3sink_prefixes_key_and_delegates_to_uploader():
    uploader = FakeUploader()
    sink = S3Sink(uploader, bucket="landing-bucket", prefix="landing/")

    sink.put("siri_et/raw/year=2026/month=5/day=4/window=123.bin", b"frames")

    # prefix is concatenated raw (house convention) and forwarded verbatim
    assert uploader.calls == [
        (
            "landing-bucket",
            "landing/siri_et/raw/year=2026/month=5/day=4/window=123.bin",
            b"frames",
        )
    ]


def test_teesink_is_local_first_and_does_not_swallow_s3_failure():
    local = FakeSink()
    tee = TeeSink([local, ExplodingSink()])  # local FIRST, S3 second

    # the S3 failure propagates — Tee does not swallow it
    with pytest.raises(RuntimeError, match="s3 is down"):
        tee.put("siri_et/raw/window=1.bin", b"payload")

    # ...but the durable local write already happened before the raise
    assert local.puts == {"siri_et/raw/window=1.bin": b"payload"}
