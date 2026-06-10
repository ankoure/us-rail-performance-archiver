from datetime import date

import pytest

from archiver.source import LocalSource, S3Source

# --------------------------------------------------------------------------- #
# Shared dataset + builders
# --------------------------------------------------------------------------- #
# One canonical dataset is materialised into BOTH backends so the contract
# tests can assert that the two implementations behave identically.
#
#   feedA -> two days (2024-01-05, 2024-01-06)
#   feedB -> one day  (2024-02-10)
#
# Note: month/day are intentionally NOT zero-padded, matching how the code
# builds and reads partition paths (f"month={day.month}" -> "month=1").

D_A1 = date(2024, 1, 5)
D_A2 = date(2024, 1, 6)
D_B1 = date(2024, 2, 10)

DATASET = {
    "feedA": {
        D_A1: {
            "metadata": b'{"id": 1}\n{"id": 2}\n',
            "bins": {"window=00.bin": b"\x00\x01", "window=01.bin": b"\x02\x03"},
        },
        D_A2: {
            "metadata": b'{"id": 3}\n',
            "bins": {"window=00.bin": b"\x10"},
        },
    },
    "feedB": {
        D_B1: {
            "metadata": b'{"id": 99}\n',
            "bins": {"window=00.bin": b"\xff"},
        },
    },
}

ALL_PAIRS = {("feedA", D_A1), ("feedA", D_A2), ("feedB", D_B1)}


class FakeUploader:
    """Minimal in-memory stand-in for the real S3 uploader."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.get_calls: list[tuple[str, str]] = []

    def put(self, key: str, data: bytes) -> None:
        self.store[key] = data

    def list_keys(self, bucket, prefix):
        # Insertion order is deliberately preserved (not sorted) so that tests
        # which depend on read_metadata's own sorting are meaningful.
        return [k for k in self.store if k.startswith(prefix)]

    def get_bytes(self, bucket, key):
        self.get_calls.append((bucket, key))
        return self.store[key]


def build_local(root, dataset):
    for feed, days in dataset.items():
        for day, payload in days.items():
            part = f"year={day.year}/month={day.month}/day={day.day}"
            md_dir = root / feed / "metadata" / part
            md_dir.mkdir(parents=True, exist_ok=True)
            (md_dir / "data.jsonl").write_bytes(payload["metadata"])

            raw_dir = root / feed / "raw" / part
            raw_dir.mkdir(parents=True, exist_ok=True)
            for name, data in payload["bins"].items():
                (raw_dir / name).write_bytes(data)
    return LocalSource(root)


def build_s3(dataset, prefix=""):
    up = FakeUploader()
    for feed, days in dataset.items():
        for day, payload in days.items():
            part = f"year={day.year}/month={day.month}/day={day.day}"
            # Store metadata as a single window file so concatenation equals
            # the original bytes -> contract parity with LocalSource.
            up.put(
                f"{prefix}{feed}/metadata/{part}/window=00.jsonl", payload["metadata"]
            )
            for name, data in payload["bins"].items():
                up.put(f"{prefix}{feed}/raw/{part}/{name}", data)
    return S3Source(up, "bucket", prefix)


# --------------------------------------------------------------------------- #
# Contract tests — run against BOTH backends
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["local", "s3"])
def source(request, tmp_path):
    if request.param == "local":
        return build_local(tmp_path, DATASET)
    return build_s3(DATASET)


def test_discover_all(source):
    assert set(source.discover()) == ALL_PAIRS


def test_discover_by_feed(source):
    assert set(source.discover(feed="feedA")) == {("feedA", D_A1), ("feedA", D_A2)}


def test_discover_by_day(source):
    assert set(source.discover(day=D_A1)) == {("feedA", D_A1)}


def test_discover_by_feed_and_day(source):
    assert set(source.discover(feed="feedA", day=D_A1)) == {("feedA", D_A1)}


def test_discover_unknown_feed_is_empty(source):
    assert set(source.discover(feed="nope")) == set()


def test_discover_unknown_day_is_empty(source):
    assert set(source.discover(day=date(1999, 1, 1))) == set()


def test_read_metadata(source):
    assert source.read_metadata("feedA", D_A1) == DATASET["feedA"][D_A1]["metadata"]
    assert source.read_metadata("feedB", D_B1) == DATASET["feedB"][D_B1]["metadata"]


def test_read_metadata_absent_day_returns_empty(source):
    assert source.read_metadata("feedA", date(1999, 1, 1)) == b""


def test_read_metadata_absent_feed_returns_empty(source):
    assert source.read_metadata("ghost", D_A1) == b""


def test_iter_bins(source):
    got = dict(source.iter_bins("feedA", D_A1))
    assert got == DATASET["feedA"][D_A1]["bins"]


def test_iter_bins_no_bins_is_empty(source):
    assert list(source.iter_bins("feedA", date(1999, 1, 1))) == []


# --------------------------------------------------------------------------- #
# LocalSource-specific tests
# --------------------------------------------------------------------------- #
def test_local_discover_empty_landing(tmp_path):
    assert list(LocalSource(tmp_path).discover()) == []


def test_local_iter_bins_ignores_non_bin_files(tmp_path):
    raw = tmp_path / "feedA" / "raw" / "year=2024/month=1/day=5"
    raw.mkdir(parents=True)
    (raw / "window=00.bin").write_bytes(b"BIN")
    (raw / "_SUCCESS").write_bytes(b"")
    (raw / "notes.txt").write_bytes(b"hi")

    src = LocalSource(tmp_path)
    assert dict(src.iter_bins("feedA", D_A1)) == {"window=00.bin": b"BIN"}


def test_local_discover_single_digit_partitions(tmp_path):
    # month=1 / day=5 (unpadded) must parse to the right date.
    src = build_local(
        tmp_path, {"f": {date(2024, 1, 5): {"metadata": b"x", "bins": {}}}}
    )
    assert set(src.discover()) == {("f", date(2024, 1, 5))}


# --------------------------------------------------------------------------- #
# S3Source-specific tests
# --------------------------------------------------------------------------- #
def test_s3_read_metadata_concatenates_in_sorted_key_order():
    up = FakeUploader()
    # Inserted out of order on purpose; read_metadata must sort by key.
    up.put("f/metadata/year=2024/month=1/day=5/window=02.jsonl", b"C")
    up.put("f/metadata/year=2024/month=1/day=5/window=00.jsonl", b"A")
    up.put("f/metadata/year=2024/month=1/day=5/window=01.jsonl", b"B")
    src = S3Source(up, "bucket")
    assert src.read_metadata("f", D_A1) == b"ABC"


def test_s3_read_metadata_ignores_non_jsonl():
    up = FakeUploader()
    up.put("f/metadata/year=2024/month=1/day=5/window=00.jsonl", b"DATA")
    up.put("f/metadata/year=2024/month=1/day=5/_SUCCESS", b"junk")
    src = S3Source(up, "bucket")
    assert src.read_metadata("f", D_A1) == b"DATA"


def test_s3_read_metadata_absent_returns_empty():
    src = S3Source(FakeUploader(), "bucket")
    assert src.read_metadata("f", D_A1) == b""


def test_s3_iter_bins_ignores_non_bin():
    up = FakeUploader()
    up.put("f/raw/year=2024/month=1/day=5/window=00.bin", b"BIN")
    up.put("f/raw/year=2024/month=1/day=5/_SUCCESS", b"")
    up.put("f/raw/year=2024/month=1/day=5/notes.txt", b"hi")
    src = S3Source(up, "bucket")
    assert dict(src.iter_bins("f", D_A1)) == {"window=00.bin": b"BIN"}


def test_s3_discover_ignores_keys_without_metadata_segment():
    up = FakeUploader()
    up.put("f/raw/year=2024/month=1/day=5/window=00.bin", b"x")
    src = S3Source(up, "bucket")
    assert src.discover() == set()


def test_s3_discover_skips_malformed_partition_layouts():
    up = FakeUploader()
    up.put("f/metadata/not-a-partition/data.jsonl", b"x")  # missing year/month/day
    up.put("f/metadata/year=2024/month=bad/day=5/window=00.jsonl", b"y")  # int() fails
    src = S3Source(up, "bucket")
    assert src.discover() == set()


def test_s3_discover_collapses_many_windows_to_one_pair():
    up = FakeUploader()
    for w in range(3):
        up.put(f"f/metadata/year=2024/month=1/day=5/window=0{w}.jsonl", b"x")
    src = S3Source(up, "bucket")
    assert src.discover() == {("f", D_A1)}


def test_s3_prefix_is_stripped_for_feed_parsing():
    src = build_s3(DATASET, prefix="landing/zone/")
    assert src.discover() == ALL_PAIRS
    assert src.read_metadata("feedA", D_A1) == DATASET["feedA"][D_A1]["metadata"]
    assert dict(src.iter_bins("feedA", D_A1)) == DATASET["feedA"][D_A1]["bins"]


def test_s3_discover_optimization_matches_full_scan():
    # When feed+day are both pinned, discover narrows the list prefix.
    # Result must equal filtering an unpinned scan.
    src = build_s3(DATASET)
    pinned = src.discover(feed="feedA", day=D_A1)
    full = {p for p in src.discover() if p == ("feedA", D_A1)}
    assert pinned == full == {("feedA", D_A1)}
