from datetime import date

import pytest

from archiver.source import (
    LocalSource,
    S3Source,
    dedupe_metadata_lines,
    select_metadata_names,
)
import gzip
import hashlib
import io
import logging
import os
import tarfile

import zstandard


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
RAW_A1 = "f/raw/year=2024/month=1/day=5/"
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


def test_iter_metadata_dedupes_to_read_metadata(source):
    # Per-file form (for the cold tarball) must hold the same rows as the
    # concatenated stream (for the rollup) once the two shared read-side
    # rules are applied: file selection, then row dedupe. iter_metadata ships
    # every file untouched; read_metadata reads the selected ones and drops
    # repeated polls, regardless of backend file naming.
    got = dict(source.iter_metadata("feedA", D_A1))
    assert got, "iter_metadata yielded nothing"
    joined = dedupe_metadata_lines(got[k] for k in select_metadata_names(got))
    assert joined == source.read_metadata("feedA", D_A1)


def test_iter_metadata_no_metadata_is_empty(source):
    assert list(source.iter_metadata("feedA", date(1999, 1, 1))) == []


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


# A day as it lands on the poller box: the append-only daily data.jsonl plus
# the window slices re-serialized from it (compact, sort_keys — see
# LandingUploader._materialize_window_metadata), so the slices are the same
# rows but not the same bytes.
MIXED_DAY_FILES = {
    "data.jsonl": b'{"id": 1}\n{"id": 2}\n',
    "window=00.jsonl": b'{"id":1}\n',
    "window=01.jsonl": b'{"id":2}\n',
}


def _write_metadata_files(root, feed, day, files):
    md = root / feed / "metadata" / f"year={day.year}/month={day.month}/day={day.day}"
    md.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (md / name).write_bytes(data)


def test_local_read_metadata_window_and_hour_only_day(tmp_path):
    # Cold tarballs from 6/11 on carry no data.jsonl — just the slices.
    _write_metadata_files(
        tmp_path,
        "f",
        D_A1,
        {"window=01.jsonl": b"B\n", "hour=00.jsonl": b"A\n", "window=02.jsonl": b"C\n"},
    )
    assert LocalSource(tmp_path).read_metadata("f", D_A1) == b"A\nB\nC\n"


def test_local_discover_yields_a_window_day_once(tmp_path):
    # list, not set: a set() would hide the duplicates this is checking for.
    _write_metadata_files(
        tmp_path, "f", D_A1, {f"window=0{w}.jsonl": b"x\n" for w in range(3)}
    )
    assert list(LocalSource(tmp_path).discover()) == [("f", D_A1)]


def test_local_mixed_day_reads_only_data_jsonl(tmp_path):
    _write_metadata_files(tmp_path, "f", D_A1, MIXED_DAY_FILES)
    src = LocalSource(tmp_path)
    assert src.read_metadata("f", D_A1) == MIXED_DAY_FILES["data.jsonl"]


def test_local_mixed_day_iter_metadata_keeps_every_file(tmp_path):
    # Pins the cold-archive decision: selection happens on read, never on
    # ship, so a tarball keeps the slices alongside data.jsonl.
    _write_metadata_files(tmp_path, "f", D_A1, MIXED_DAY_FILES)
    assert dict(LocalSource(tmp_path).iter_metadata("f", D_A1)) == MIXED_DAY_FILES


# --------------------------------------------------------------------------- #
# select_metadata_names
# --------------------------------------------------------------------------- #
def test_select_prefers_data_jsonl_when_present():
    names = ["window=01.jsonl", "data.jsonl", "hour=00.jsonl"]
    assert select_metadata_names(names) == ["data.jsonl"]


def test_select_sorts_slices_when_no_data_jsonl():
    names = ["window=02.jsonl", "hour=00.jsonl", "window=01.jsonl"]
    assert select_metadata_names(names) == [
        "hour=00.jsonl",
        "window=01.jsonl",
        "window=02.jsonl",
    ]


def test_select_empty_is_empty():
    assert select_metadata_names([]) == []


# --------------------------------------------------------------------------- #
# dedupe_metadata_lines
# --------------------------------------------------------------------------- #
# One poll as two writers serialize it: the legacy hour= file in json.dumps
# defaults, the window= slice compact with sort_keys. Same row, different
# bytes, which is why the dedupe has to parse rather than compare lines.
HOUR_ROW = b'{"timestamp": 1788739215.744962, "status": 200, "digest": "abc123"}\n'
WINDOW_ROW = b'{"digest":"abc123","status":200,"timestamp":1788739215.744962}\n'


def test_dedupe_drops_cross_format_duplicate_and_keeps_first_bytes():
    # The first occurrence's ORIGINAL line survives; nothing is re-serialized.
    assert dedupe_metadata_lines([HOUR_ROW, WINDOW_ROW]) == HOUR_ROW
    assert dedupe_metadata_lines([WINDOW_ROW, HOUR_ROW]) == WINDOW_ROW


def test_dedupe_drops_duplicate_within_one_chunk():
    assert dedupe_metadata_lines([HOUR_ROW + WINDOW_ROW]) == HOUR_ROW


def test_dedupe_keeps_same_digest_at_different_timestamps():
    # A 304 repeats the previous digest: one digest, many real polls. Fails
    # if the key is digest alone.
    rows = [
        b'{"timestamp": 100.0, "status": 200, "digest": "abc123"}\n',
        b'{"timestamp": 160.0, "status": 304, "digest": "abc123"}\n',
        b'{"timestamp": 220.0, "status": 304, "digest": "abc123"}\n',
    ]
    assert dedupe_metadata_lines(rows) == b"".join(rows)


def test_dedupe_keeps_different_digests_at_one_timestamp():
    # Fails if the key is timestamp alone.
    rows = [
        b'{"timestamp": 100.0, "digest": "abc123"}\n',
        b'{"timestamp": 100.0, "digest": "def456"}\n',
    ]
    assert dedupe_metadata_lines(rows) == b"".join(rows)


def test_dedupe_keeps_error_rows_without_a_digest():
    rows = [
        b'{"timestamp": 100.0, "error": "timeout"}\n',
        b'{"timestamp": 160.0, "error": "timeout"}\n',
    ]
    assert dedupe_metadata_lines(rows) == b"".join(rows)


def test_dedupe_collapses_a_repeated_error_row():
    # No digest keys as (timestamp, None), so the same failed poll shipped
    # twice still comes out once. A missing digest and an explicit null are
    # the same key.
    first = b'{"timestamp": 100.0, "error": "timeout"}\n'
    again = b'{"digest":null,"error":"timeout","timestamp":100.0}\n'
    assert dedupe_metadata_lines([first, again]) == first


def test_dedupe_does_not_confuse_an_error_row_with_a_poll_at_that_timestamp():
    rows = [
        b'{"timestamp": 100.0, "error": "timeout"}\n',
        b'{"timestamp": 100.0, "digest": "abc123"}\n',
    ]
    assert dedupe_metadata_lines(rows) == b"".join(rows)


def test_dedupe_matches_timestamps_that_differ_below_a_microsecond():
    # Two writers can print one instant with different digit counts. These
    # two literals parse to DIFFERENT doubles (asserted, so the test stays
    # meaningful), which is why the key is integer microseconds, not the float.
    assert float("1788739215.744962") != float("1788739215.7449622")
    first = b'{"timestamp": 1788739215.744962, "digest": "abc123"}\n'
    again = b'{"timestamp": 1788739215.7449622, "digest": "abc123"}\n'
    assert dedupe_metadata_lines([first, again]) == first


def test_dedupe_keeps_polls_one_microsecond_apart():
    rows = [
        b'{"timestamp": 1788739215.744962, "digest": "abc123"}\n',
        b'{"timestamp": 1788739215.744963, "digest": "abc123"}\n',
    ]
    assert dedupe_metadata_lines(rows) == b"".join(rows)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b'{"timestamp": 100.0, "digest": "abc', id="truncated-json"),
        pytest.param(b"not json at all", id="garbage"),
        pytest.param(b'{"digest": "abc123"}', id="no-timestamp"),
        pytest.param(b'{"timestamp": "soon", "digest": "abc123"}', id="bad-timestamp"),
        pytest.param(b'{"timestamp": null, "digest": "abc123"}', id="null-timestamp"),
        pytest.param(b'{"timestamp": 100.0, "digest": ["a"]}', id="non-string-digest"),
        pytest.param(b"[1, 2, 3]", id="not-an-object"),
    ],
)
def test_dedupe_passes_unkeyable_lines_through_every_time(line):
    # A line with no key is never dropped, even when it repeats: discarding
    # data is the caller's decision (the rollup fails on it, digest_timestamps
    # warns and skips), not read_metadata's.
    good = b'{"timestamp": 100.0, "digest": "abc123"}\n'
    got = dedupe_metadata_lines([line + b"\n" + good, line + b"\n"])
    assert got == line + b"\n" + good + line + b"\n"


def test_dedupe_without_duplicates_is_plain_concatenation():
    chunks = [
        b'{"timestamp": 100.0, "digest": "a"}\n{"timestamp": 160.0, "digest": "b"}\n',
        b'{"timestamp": 220.0, "digest": "c"}\n',
    ]
    assert dedupe_metadata_lines(chunks) == b"".join(chunks)


def test_dedupe_does_not_fuse_rows_across_an_unterminated_chunk():
    # A file missing its final newline used to glue its last row onto the
    # next file's first row under a bare b"".join.
    first = b'{"timestamp": 100.0, "digest": "a"}'
    second = b'{"timestamp": 160.0, "digest": "b"}\n'
    assert dedupe_metadata_lines([first, second]) == first + b"\n" + second


def test_dedupe_drops_blank_lines():
    row = b'{"timestamp": 100.0, "digest": "a"}\n'
    assert dedupe_metadata_lines([b"\n" + row + b"\n  \n"]) == row


def test_dedupe_empty_input_is_empty():
    assert dedupe_metadata_lines([]) == b""
    assert dedupe_metadata_lines([b""]) == b""


def test_dedupe_accepts_a_one_shot_generator():
    # Both read_metadata call sites pass a generator, so files are read (and
    # S3 objects fetched) lazily, one at a time.
    assert dedupe_metadata_lines(c for c in [HOUR_ROW, WINDOW_ROW]) == HOUR_ROW


# --------------------------------------------------------------------------- #
# read_metadata dedupe — run against BOTH backends
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["local", "s3"])
def metadata_source(request, tmp_path):
    """Build a source holding one feed-day ("f", D_A1) of the given metadata
    files, so each test states its files once and both backends read them."""

    def build(files):
        if request.param == "local":
            _write_metadata_files(tmp_path, "f", D_A1, files)
            return LocalSource(tmp_path)
        up = FakeUploader()
        for name, data in files.items():
            up.put(f"f/metadata/year=2024/month=1/day=5/{name}", data)
        return S3Source(up, "bucket")

    return build


def test_read_metadata_drops_hour_and_window_copies_of_one_poll(metadata_source):
    # The switch days: every closed window was re-shipped as window= files on
    # top of the hour= objects already there. hour= sorts first, so its bytes
    # are the ones kept.
    earlier = b'{"timestamp": 1788739155.1, "status": 200, "digest": "aaa111"}\n'
    later = b'{"digest":"ccc333","status":200,"timestamp":1788739275.9}\n'
    src = metadata_source(
        {
            "window=1788739200.jsonl": WINDOW_ROW + later,
            "hour=01.jsonl": earlier + HOUR_ROW,
        }
    )
    assert src.read_metadata("f", D_A1) == earlier + HOUR_ROW + later


def test_read_metadata_drops_rows_shared_by_overlapping_windows(metadata_source):
    # A ship_window_seconds change: the old wide window and the new narrow
    # ones cover the same polls.
    a = b'{"digest":"aaa111","timestamp":100.0}\n'
    b = b'{"digest":"bbb222","timestamp":400.0}\n'
    c = b'{"digest":"ccc333","timestamp":700.0}\n'
    src = metadata_source(
        {
            "window=0000.jsonl": a + b + c,
            "window=0300.jsonl": b,
            "window=0600.jsonl": c,
        }
    )
    assert src.read_metadata("f", D_A1) == a + b + c


def test_read_metadata_dedupes_data_jsonl_too(metadata_source):
    # One rule on every branch: data.jsonl should never hold a duplicate, but
    # if it does it is not exempt.
    src = metadata_source({"data.jsonl": HOUR_ROW + WINDOW_ROW})
    assert src.read_metadata("f", D_A1) == HOUR_ROW


def test_read_metadata_keeps_304s_across_files(metadata_source):
    first = b'{"digest":"abc123","status":200,"timestamp":100.0}\n'
    not_modified = b'{"digest":"abc123","status":304,"timestamp":160.0}\n'
    src = metadata_source(
        {"window=0000.jsonl": first, "window=0100.jsonl": not_modified}
    )
    assert src.read_metadata("f", D_A1) == first + not_modified


def test_iter_metadata_still_ships_duplicated_files_untouched(metadata_source):
    # Dedupe is read-side only, like selection: the cold tarball keeps every
    # file exactly as written.
    files = {"hour=01.jsonl": HOUR_ROW, "window=1788739200.jsonl": WINDOW_ROW}
    src = metadata_source(files)
    assert dict(src.iter_metadata("f", D_A1)) == files


# --------------------------------------------------------------------------- #
# S3Source-specific tests
# --------------------------------------------------------------------------- #
def test_s3_read_metadata_concatenates_in_sorted_key_order():
    up = FakeUploader()
    # Inserted out of order on purpose; read_metadata must sort by key.
    up.put("f/metadata/year=2024/month=1/day=5/window=02.jsonl", b"C\n")
    up.put("f/metadata/year=2024/month=1/day=5/window=00.jsonl", b"A\n")
    up.put("f/metadata/year=2024/month=1/day=5/window=01.jsonl", b"B\n")
    src = S3Source(up, "bucket")
    assert src.read_metadata("f", D_A1) == b"A\nB\nC\n"


def test_s3_read_metadata_ignores_non_jsonl():
    up = FakeUploader()
    up.put("f/metadata/year=2024/month=1/day=5/window=00.jsonl", b"DATA\n")
    up.put("f/metadata/year=2024/month=1/day=5/_SUCCESS", b"junk")
    src = S3Source(up, "bucket")
    assert src.read_metadata("f", D_A1) == b"DATA\n"


def test_s3_read_metadata_absent_returns_empty():
    src = S3Source(FakeUploader(), "bucket")
    assert src.read_metadata("f", D_A1) == b""


def test_s3_mixed_day_reads_only_data_jsonl():
    # Same shared rule as LocalSource. S3 landing never holds data.jsonl
    # today, so this guards the rule, not a live case.
    up = FakeUploader()
    for name, data in MIXED_DAY_FILES.items():
        up.put(f"f/metadata/year=2024/month=1/day=5/{name}", data)
    src = S3Source(up, "bucket")
    assert src.read_metadata("f", D_A1) == MIXED_DAY_FILES["data.jsonl"]


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


def make_tar(
    members: dict[str, bytes],
    *,
    compressed: bool = False,
    dirs: tuple[str, ...] = (),
    symlinks: dict[str, str] | None = None,
) -> bytes:
    """Build tar bytes in memory for the .tar objects _ship_raw_window writes.

    Directories and symlinks are separate parameters rather than sentinel
    values in `members` because neither has contents of its own — encoding
    them as None in a dict[str, bytes] would make the type a lie.

    `compressed=True` writes gzip, which the caller still stores under a .tar
    key: exactly the mismatch mode="r:" exists to reject and "r:*" would
    silently decode.

    mtime is pinned so equal members produce equal bytes, which the cross-tar
    dedup test relies on.
    """
    symlinks = symlinks or {}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if compressed else "w") as tar:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mtime = 0
            tar.addfile(info)
        for name, target in symlinks.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mtime = 0
            tar.addfile(info)
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_s3_iter_bins_unpacks_tar_into_members():
    """A .tar key yields its members, never the tar itself as one blob."""
    up = FakeUploader()
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar",
        make_tar({"aaa.bin": b"alpha", "bbb.bin": b"beta"}),
    )
    src = S3Source(up, "bucket")

    assert dict(src.iter_bins("f", D_A1)) == {"aaa.bin": b"alpha", "bbb.bin": b"beta"}


def test_s3_iter_bins_mixes_legacy_bins_and_tars():
    """Both object shapes come out of one call, as during the migration."""
    up = FakeUploader()
    up.put(RAW_A1 + "window=00.bin", b"LEGACY")
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar",
        make_tar({"aaa.bin": b"alpha"}),
    )
    src = S3Source(up, "bucket")

    assert dict(src.iter_bins("f", D_A1)) == {
        "window=00.bin": b"LEGACY",
        "aaa.bin": b"alpha",
    }


def test_s3_iter_bins_ignores_unknown_extensions_beside_a_tar():
    """Unrecognised keys are filtered on the name, before any GET."""
    up = FakeUploader()
    up.put(RAW_A1 + "_SUCCESS", b"")
    up.put(RAW_A1 + "notes.txt", b"hi")
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar",
        make_tar({"aaa.bin": b"alpha"}),
    )
    src = S3Source(up, "bucket")

    assert dict(src.iter_bins("f", D_A1)) == {"aaa.bin": b"alpha"}
    # never fetched, not fetched-then-discarded
    assert [k for _, k in up.get_calls] == [
        RAW_A1 + "window=1700000000--shipped=1700003600.tar"
    ]


def test_s3_iter_bins_unpacks_tar_gz_into_members():
    """A .tar.gz key (current _ship_raw_window format) unpacks the same way."""
    up = FakeUploader()
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar.gz",
        make_tar({"aaa.bin": b"alpha", "bbb.bin": b"beta"}, compressed=True),
    )
    src = S3Source(up, "bucket")

    assert dict(src.iter_bins("f", D_A1)) == {"aaa.bin": b"alpha", "bbb.bin": b"beta"}


def test_s3_iter_bins_mixes_legacy_tar_and_current_tar_gz():
    """Both formats come out of one call, as during the .tar -> .tar.gz cutover."""
    up = FakeUploader()
    up.put(
        RAW_A1 + "window=00--shipped=00.tar",
        make_tar({"aaa.bin": b"alpha"}),
    )
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar.gz",
        make_tar({"bbb.bin": b"beta"}, compressed=True),
    )
    src = S3Source(up, "bucket")

    assert dict(src.iter_bins("f", D_A1)) == {"aaa.bin": b"alpha", "bbb.bin": b"beta"}


def test_s3_iter_bins_skips_ungzipped_tar_under_gz_key_loudly(caplog):
    """Mirror of the .tar mismatch test: mode="r:gz" rejects plain tar bytes.

    An uncompressed tar stored under a .tar.gz key must raise ReadError
    (logged, object skipped), not be silently accepted.
    """
    up = FakeUploader()
    bad_key = RAW_A1 + "window=1700000000--shipped=1700003600.tar.gz"
    up.put(bad_key, make_tar({"aaa.bin": b"alpha"}, compressed=False))
    up.put(RAW_A1 + "window=00.bin", b"LEGACY")
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.ERROR):
        assert dict(src.iter_bins("f", D_A1)) == {"window=00.bin": b"LEGACY"}

    assert "Unreadable raw tar" in caplog.text
    assert bad_key in caplog.text


def test_s3_iter_bins_skips_gzipped_tar_loudly_and_keeps_going(caplog):
    """The reason mode="r:" was chosen over "r:*".

    A gzip'd tar under a .tar key must raise ReadError (logged, object
    skipped), not be silently auto-decoded. The later .bin proves one bad
    object costs only itself — iteration is not abandoned.
    """
    up = FakeUploader()
    bad_key = RAW_A1 + "window=1700000000--shipped=1700003600.tar"
    up.put(bad_key, make_tar({"aaa.bin": b"alpha"}, compressed=True))
    up.put(RAW_A1 + "window=00.bin", b"LEGACY")
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.ERROR):
        assert dict(src.iter_bins("f", D_A1)) == {"window=00.bin": b"LEGACY"}

    assert "Unreadable raw tar" in caplog.text
    assert bad_key in caplog.text
    # the same bytes are readable under "r:*" — that is what is being refused
    with tarfile.open(fileobj=io.BytesIO(up.store[bad_key]), mode="r:*") as tar:
        assert tar.getnames() == ["aaa.bin"]


def test_s3_iter_bins_skips_non_file_members():
    """Directory and symlink members carry no payload of their own.

    The symlink is the load-bearing case: extractfile() returns None for a
    directory, so the `extracted is None` guard would cover that on its own,
    but for a symlink it returns the *target's* bytes — only the isfile()
    check stops "link.bin" being yielded as a second copy of aaa.bin.
    """
    up = FakeUploader()
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar",
        make_tar(
            {"aaa.bin": b"alpha"},
            dirs=("subdir",),
            symlinks={"link.bin": "aaa.bin"},
        ),
    )
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == [("aaa.bin", b"alpha")]


def test_s3_iter_bins_skips_non_flat_member_names(caplog):
    """arcname was path.name, so anything with a path in it is not ours.

    getmembers() does not sanitise names — only the extraction filters do — so
    these really do reach the loop and the check is load-bearing.
    """
    up = FakeUploader()
    key = RAW_A1 + "window=1700000000--shipped=1700003600.tar"
    up.put(
        key,
        make_tar(
            {
                "nested/inner.bin": b"nested",
                "../escape.bin": b"escape",
                ".": b"dot",
                "..": b"dotdot",
                "aaa.bin": b"alpha",
            }
        ),
    )
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.WARNING):
        assert list(src.iter_bins("f", D_A1)) == [("aaa.bin", b"alpha")]

    skipped = [r for r in caplog.records if "non-flat member" in r.getMessage()]
    assert len(skipped) == 4


def test_s3_iter_bins_dedups_a_member_across_two_tars():
    """A retry ships a second tar with overlapping members (see
    landing_uploader._ship_raw_window): the repeat must be yielded once.

    Asserted on the ordered list of names, not a dict — a dict would collapse
    a double-yield and hide exactly the regression this pins.
    """
    up = FakeUploader()
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar",
        make_tar({"dupe.bin": b"payload", "only-first.bin": b"A"}),
    )
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700007200.tar",
        make_tar({"dupe.bin": b"payload", "only-second.bin": b"B"}),
    )
    src = S3Source(up, "bucket")

    pairs = list(src.iter_bins("f", D_A1))

    assert [name for name, _ in pairs] == [
        "dupe.bin",
        "only-first.bin",
        "only-second.bin",
    ]
    assert dict(pairs)["dupe.bin"] == b"payload"


# --------------------------------------------------------------------------- #
# .tar.zst — the current _ship_raw_window format
# --------------------------------------------------------------------------- #
# make_tar_zst mirrors _ship_raw_window's writer exactly: a level-6
# stream_writer wrapped around tarfile "w|". If that writer changes shape,
# update this builder to match; test_landing_uploader's round-trip test is
# what catches a divergence between the two.
#
# Payloads that must cross zstd block boundaries use os.urandom: random bytes
# are incompressible, so compressed size tracks raw size and a byte offset into
# the blob lands predictably inside a given member. Compressible payloads would
# collapse into a single block, and truncation would then drop the whole
# object before one member decoded, hiding the bugs these tests target.

# Comfortably above zstd's 128 KiB maximum block size, so each member spans
# several blocks and a truncation can land in the middle of one.
MULTI_BLOCK = 400_000

ZST_KEY = RAW_A1 + "window=1700000000--shipped=1700003600.tar.zst"
ZST_RETRY_KEY = RAW_A1 + "window=1700000000--shipped=1700007200.tar.zst"


def make_tar_zst(members: dict[str, bytes], *, frame_per_member: bool = False) -> bytes:
    """A .tar.zst blob built the way _ship_raw_window builds one.

    frame_per_member ends a zstd frame after every member, producing a
    multi-frame object. The production writer never does this; it exists to
    pin that the reader would cope if it ever did.

    mtime is pinned for the same reason as make_tar: equal members, equal
    bytes.
    """
    buf = io.BytesIO()
    zw = zstandard.ZstdCompressor(level=6).stream_writer(buf, closefd=False)
    with tarfile.open(fileobj=zw, mode="w|") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
            if frame_per_member:
                zw.flush(zstandard.FLUSH_FRAME)
    zw.close()
    return buf.getvalue()


def digest_name(data: bytes) -> str:
    """{sha256}.bin, the arcname _ship_raw_window gives every member."""
    return f"{hashlib.sha256(data).hexdigest()}.bin"


def random_members(count: int, size: int) -> dict[str, bytes]:
    payloads = [os.urandom(size) for _ in range(count)]
    return {digest_name(p): p for p in payloads}


def test_s3_iter_bins_unpacks_tar_zst_into_members():
    """A .tar.zst key yields its members, in archive order."""
    up = FakeUploader()
    up.put(ZST_KEY, make_tar_zst({"aaa.bin": b"alpha", "bbb.bin": b"beta"}))
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == [
        ("aaa.bin", b"alpha"),
        ("bbb.bin", b"beta"),
    ]


def test_s3_iter_bins_tar_zst_frames_carry_no_content_size():
    """Pins the property that rules out one-shot ZstdDecompressor.decompress().

    stream_writer can't know the total size when it writes the frame header,
    so the header has none, and decompress() refuses such frames. iter_bins
    streams through stream_reader instead. This fails if someone "simplifies"
    the zstd branch to decompress-to-bytes.
    """
    blob = make_tar_zst({"aaa.bin": b"alpha"})
    assert zstandard.frame_content_size(blob) == -1
    with pytest.raises(zstandard.ZstdError):
        zstandard.ZstdDecompressor().decompress(blob)

    up = FakeUploader()
    up.put(ZST_KEY, blob)
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == [("aaa.bin", b"alpha")]


def test_s3_iter_bins_tar_zst_members_larger_than_a_block_round_trip():
    """Multi-block members decode byte-for-byte through the streaming reader."""
    members = random_members(3, MULTI_BLOCK)
    up = FakeUploader()
    up.put(ZST_KEY, make_tar_zst(members))
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == list(members.items())


def test_s3_iter_bins_tar_zst_reads_across_every_frame():
    """read_across_frames=False only stops a single read() at a frame edge;
    the next read() continues. If a zstandard release ever changed that, the
    reader would silently stop after the first member, and this would catch it.
    """
    members = random_members(3, 1_000)
    up = FakeUploader()
    up.put(ZST_KEY, make_tar_zst(members, frame_per_member=True))
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == list(members.items())


def test_s3_iter_bins_tar_zst_yields_a_zero_byte_member():
    """An empty payload is still a payload with a digest and must survive."""
    members = {digest_name(b""): b"", digest_name(b"after"): b"after"}
    up = FakeUploader()
    up.put(ZST_KEY, make_tar_zst(members))
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == list(members.items())


def test_s3_iter_bins_empty_tar_zst_is_not_an_error(caplog):
    up = FakeUploader()
    up.put(ZST_KEY, make_tar_zst({}))
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.ERROR):
        assert list(src.iter_bins("f", D_A1)) == []
    assert "Unreadable raw tar" not in caplog.text


@pytest.mark.parametrize(
    "bad_blob",
    [
        pytest.param(b"definitely not zstd", id="garbage"),
        pytest.param(make_tar({"aaa.bin": b"alpha"}, compressed=True), id="gzip-tar"),
        pytest.param(make_tar({"aaa.bin": b"alpha"}), id="plain-tar"),
        pytest.param(gzip.compress(b"x" * 100), id="gzip-non-tar"),
    ],
)
def test_s3_iter_bins_skips_non_zstd_bytes_under_zst_key_loudly(bad_blob, caplog):
    """Suffix decides the decoder, so wrong bytes under a .tar.zst key must
    fail loudly (logged, object skipped) for that object alone. The later .bin
    proves iteration is not abandoned.
    """
    up = FakeUploader()
    up.put(ZST_KEY, bad_blob)
    up.put(RAW_A1 + "window=00.bin", b"LEGACY")
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.ERROR):
        assert dict(src.iter_bins("f", D_A1)) == {"window=00.bin": b"LEGACY"}

    assert "Unreadable raw tar" in caplog.text
    assert ZST_KEY in caplog.text


def test_s3_iter_bins_dedups_a_member_across_tar_gz_and_tar_zst():
    """During the gzip -> zstd cutover one window can exist in both formats.
    seen_members is shared across branches, so the overlap comes out once.
    """
    up = FakeUploader()
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar.gz",
        make_tar({"dupe.bin": b"payload", "only-gz.bin": b"G"}, compressed=True),
    )
    up.put(ZST_RETRY_KEY, make_tar_zst({"dupe.bin": b"payload", "only-zst.bin": b"Z"}))
    src = S3Source(up, "bucket")

    assert [name for name, _ in src.iter_bins("f", D_A1)] == [
        "dupe.bin",
        "only-gz.bin",
        "only-zst.bin",
    ]


def test_s3_iter_bins_truncated_tar_zst_keeps_members_before_the_break(caplog):
    """Streaming mode yields as it goes: a member fully decoded before the
    truncation point is kept, and the object is logged rather than raised.
    """
    members = random_members(3, MULTI_BLOCK)
    blob = make_tar_zst(members)
    up = FakeUploader()
    up.put(ZST_KEY, blob[: len(blob) // 2])  # breaks inside the second member
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.ERROR):
        assert list(src.iter_bins("f", D_A1)) == list(members.items())[:1]

    assert "Unreadable raw tar" in caplog.text
    assert ZST_KEY in caplog.text


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param(0.05, id="breaks-in-first-member"),
        pytest.param(0.5, id="breaks-in-second-member"),
        pytest.param(0.9, id="breaks-in-third-member"),
    ],
)
def test_s3_iter_bins_truncated_tar_does_not_suppress_the_intact_retry(cut, caplog):
    """A member whose read failed must not be marked as seen.

    The first tar is cut off inside one member's data: that member's header
    decodes but its bytes don't. The second tar is the intact retry and holds
    every member, so every member must come out exactly once.

    Fails if a name is added to seen_members before its bytes are read: the
    failed member is then skipped as a "duplicate" when the retry tar reaches
    it, and its only good copy is silently dropped.

    Insertion order matters: FakeUploader lists in insertion order, so the
    truncated tar is read first, as it would be in S3 where its earlier
    shipped= stamp sorts first.
    """
    members = random_members(3, MULTI_BLOCK)
    blob = make_tar_zst(members)
    up = FakeUploader()
    up.put(ZST_KEY, blob[: int(len(blob) * cut)])
    up.put(ZST_RETRY_KEY, blob)
    src = S3Source(up, "bucket")

    with caplog.at_level(logging.ERROR):
        pairs = list(src.iter_bins("f", D_A1))

    # a list, not a dict: a dict would collapse a double-yield
    assert sorted(pairs) == sorted(members.items())
    assert "Unreadable raw tar" in caplog.text  # the damage is still reported


def test_s3_iter_bins_mixes_all_four_object_shapes():
    """Legacy flat .bin, legacy .tar, legacy .tar.gz and current .tar.zst can
    all sit under one day prefix, and all come out of one call.
    """
    up = FakeUploader()
    up.put(RAW_A1 + "window=00.bin", b"LEGACY")
    up.put(
        RAW_A1 + "window=1700000000--shipped=1700003600.tar",
        make_tar({"plain.bin": b"P"}),
    )
    up.put(
        RAW_A1 + "window=1700000300--shipped=1700003900.tar.gz",
        make_tar({"gzipped.bin": b"G"}, compressed=True),
    )
    up.put(
        RAW_A1 + "window=1700000600--shipped=1700004200.tar.zst",
        make_tar_zst({"zstd.bin": b"Z"}),
    )
    src = S3Source(up, "bucket")

    assert list(src.iter_bins("f", D_A1)) == [
        ("window=00.bin", b"LEGACY"),
        ("plain.bin", b"P"),
        ("gzipped.bin", b"G"),
        ("zstd.bin", b"Z"),
    ]
