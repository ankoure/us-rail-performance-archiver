"""analysis/curated_fs.py — resolving a curated root to a pyarrow filesystem.

Only the local backend is exercised here; the S3 backend is the same pyarrow
FileSystem interface and hitting it would need network + credentials in CI. What
these tests pin down is the behaviour the readers depend on: a missing prefix is
an EMPTY result rather than an exception (so "no partition for this feed" stays a
skip), and non-parquet files are ignored.
"""

from __future__ import annotations

from pyarrow.fs import LocalFileSystem

import analysis.curated_fs as curated_fs_mod
from analysis.curated_fs import curated_fs, has_any, list_parquet


def test_local_path_resolves_to_localfilesystem_and_absolute_path(tmp_path):
    fs, base = curated_fs(tmp_path / "curated")
    assert isinstance(fs, LocalFileSystem)
    assert base == str((tmp_path / "curated").resolve())


def test_relative_local_path_is_made_absolute():
    # FileSystem.from_uri rejects a bare relative path ("URI has empty scheme"),
    # which is why curated_fs resolves rather than round-tripping through a URI.
    fs, base = curated_fs("data/curated")
    assert isinstance(fs, LocalFileSystem)
    assert base.startswith("/") and base.endswith("data/curated")


def test_uri_with_a_scheme_is_delegated_to_pyarrow(monkeypatch):
    """Routing only -- deliberately does NOT construct a real S3FileSystem.

    `FileSystem.from_uri("s3://...")` issues a HeadBucket to resolve the
    bucket's region, so a test that built one for real would need network and
    credentials in CI (and fails outright on a short/invalid bucket name).
    """
    seen = []

    class FakeFileSystem:
        # pyarrow's FileSystem is a Cython extension type whose attributes can't
        # be set, so patch the module-level name curated_fs() resolves instead.
        @staticmethod
        def from_uri(uri):
            seen.append(uri)
            return ("FS", "some-bucket/prefix")

    monkeypatch.setattr(curated_fs_mod, "FileSystem", FakeFileSystem)
    fs, base = curated_fs("s3://some-bucket/prefix")

    assert seen == ["s3://some-bucket/prefix"]
    assert (fs, base) == ("FS", "some-bucket/prefix")


def test_list_parquet_missing_prefix_is_empty_not_an_error(tmp_path):
    """The readers turn [] into FileNotFoundError themselves; if this raised
    instead, a feed with no partition would become a hard failure."""
    fs, base = curated_fs(tmp_path)
    assert list_parquet(fs, f"{base}/nope/feed=absent") == []


def test_list_parquet_sorted_and_parquet_only(tmp_path):
    part = tmp_path / "vehicles" / "feed=x" / "year=2026" / "month=9" / "day=2"
    part.mkdir(parents=True)
    (part / "b.parquet").write_bytes(b"PAR1")
    (part / "a.parquet").write_bytes(b"PAR1")
    (part / "data.parquet.tmp").write_bytes(b"partial")
    (part / "README").write_text("not data")

    fs, base = curated_fs(tmp_path)
    got = list_parquet(fs, f"{base}/vehicles/feed=x/year=2026/month=9/day=2")

    assert [p.rsplit("/", 1)[-1] for p in got] == ["a.parquet", "b.parquet"]


def test_has_any_detects_feed_partitions(tmp_path):
    fs, base = curated_fs(tmp_path)
    assert has_any(fs, f"{base}/vehicles") is False

    (tmp_path / "vehicles" / "feed=x").mkdir(parents=True)
    assert has_any(fs, f"{base}/vehicles") is True


def test_has_any_ignores_non_matching_children(tmp_path):
    (tmp_path / "vehicles" / "_tmp").mkdir(parents=True)
    fs, base = curated_fs(tmp_path)
    assert has_any(fs, f"{base}/vehicles") is False


def test_file_scheme_also_goes_through_from_uri(tmp_path):
    # file:// is the one scheme that can be resolved for real without network.
    fs, base = curated_fs(f"file://{tmp_path}")
    assert isinstance(fs, LocalFileSystem)
    assert base == str(tmp_path)
