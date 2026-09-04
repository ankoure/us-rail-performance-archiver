"""Resolving a curated root to a pyarrow filesystem + path.

The curated tree is addressable two ways that must behave identically:

  data/curated                          local disk (dev, and the combined task)
  s3://rail-performance-archiver-hot    the prod hot bucket

Those are the same tree. `Shipper._hot_key` is literally
`parquet.relative_to(curated_dir)`, so a partition that lives locally at
`<curated>/vehicles/feed=X/year=2026/month=9/day=2/data.parquet` is at exactly
that key under the hot bucket. That 1:1 mapping is what lets gold run in its own
ECS task -- reading silver over S3 -- instead of needing shared local disk with
rollup, which is what sank the 2026-07-31 stage split (see the NOTE in
terraform/rollup.tf).

Why pyarrow's own filesystem rather than a bytes-oriented seam like
`archiver.source.Source`: `Source.iter_bins` returns `bytes`, which is fine for
landing's small framed bins but would force a whole parquet file into memory.
`TripUpdatesDay` and `pipeline/compact_trip_updates.py` deliberately read one
row group at a time to bound peak memory (metromn-trips' biggest day is 70M+
rows across 3,144 row groups). `S3FileSystem.open_input_file` returns a seekable
NativeFile, so `pq.ParquetFile(...).read_row_group(i)` issues ranged GETs and
that batching survives the move to S3 unchanged.

Using `LocalFileSystem` for the local case rather than branching on scheme at
each call site means there is ONE code path through the readers, so the local
and S3 behaviours can't drift.
"""

from __future__ import annotations

from pathlib import Path

from pyarrow.fs import FileSelector, FileSystem, LocalFileSystem


def curated_fs(base: Path | str) -> tuple[FileSystem, str]:
    """Resolve a curated root to `(filesystem, base_path)`.

    A value containing "://" is treated as a URI and handed to pyarrow (so
    `s3://bucket/prefix` yields an S3FileSystem and `bucket/prefix`); anything
    else is a local path. `FileSystem.from_uri` rejects a bare relative path
    ("URI has empty scheme"), so local paths are resolved to absolute here
    rather than round-tripped through a file:// URI.
    """
    text = str(base)
    if "://" in text:
        return FileSystem.from_uri(text)
    return LocalFileSystem(), str(Path(text).resolve())


def list_parquet(fs: FileSystem, prefix: str) -> list[str]:
    """Sorted .parquet paths directly under `prefix`; [] if it doesn't exist.

    Replaces `sorted(path.glob("*.parquet"))`. `allow_not_found` keeps a missing
    partition a normal empty result -- callers raise FileNotFoundError
    themselves, so the "no partition for this feed" path stays a skip rather
    than becoming an error.
    """
    selector = FileSelector(prefix, allow_not_found=True)
    return sorted(
        info.path
        for info in fs.get_file_info(selector)
        if info.path.endswith(".parquet")
    )


def has_any(fs: FileSystem, prefix: str, pattern: str = "feed=") -> bool:
    """Whether `prefix` holds at least one child whose basename starts with
    `pattern` -- the cheap existence probe behind gold's empty-tree guard."""
    selector = FileSelector(prefix, allow_not_found=True)
    return any(
        info.base_name.startswith(pattern) for info in fs.get_file_info(selector)
    )
