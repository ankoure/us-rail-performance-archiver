"""Rewrite hot `metadata` partitions from the inferred era to METADATA_SCHEMA.

Until archiver/metadata_schema.py, `_rollup_metadata` inferred each feed-day's
schema with `paj.read_json`: columns came and went with the day's rows,
`timestamp` was a float of epoch seconds, and the column order flipped to
alphabetical once window slices took over. Those partitions carry no
`schema_version` in their key-value metadata (version 0 here). This rewrites
each of them through the same `metadata_table()` the rollup now uses, so a
rewritten partition is identical by construction to one the rollup would build
today. Rows are deduplicated on the same (to_us(timestamp), digest) key as
`read_metadata`, keeping the first occurrence, which removes the 9/7–9/8
window re-ship duplicates. Cold is untouched: its jsonl is the source of truth
these partitions were built from.

Runbook:

    # 1. inventory: partitions by schema_version, every column and type seen,
    #    and any column METADATA_SCHEMA doesn't have (stop there if so: the
    #    schema needs the column, as a version bump, before anything is rewritten)
    uv run python pipeline/rewrite_metadata_schema.py --footers-only

    # 2. dry run (the default): converts every stale partition in memory and
    #    reports what would fail, duplicates removed and rows skipped. No writes.
    uv run python pipeline/rewrite_metadata_schema.py

    # 3. rewrite in place. Each original is first copied to
    #    _backup/metadata-v0/<key> (the hot bucket isn't versioned).
    uv run python pipeline/rewrite_metadata_schema.py --apply

    # 4. confirm nothing stale is left (a nightly on the old image can write
    #    one more version-0 partition; re-run --apply if so), then delete the
    #    backup prefix once readers check out.
    uv run python pipeline/rewrite_metadata_schema.py --footers-only

Run step 3 after the schema change is deployed, so no new version-0 partitions
appear behind it. The rollup's hot completion marker still records each day's
pre-dedupe metadata row count; nothing compares that count with the parquet
(prune only checks the marker exists), so it's left alone.

Writes go to a temp key, then a server-side copy over the live key, then the
temp key is deleted, never a direct overwrite (same as
backfill_compact_trip_updates.py). A partition is read back after its write and
the run fails if it isn't at the current version with the expected row count.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# Make the repo root importable when run as `python pipeline/rewrite_metadata_schema.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from pyarrow import fs as pafs  # noqa: E402

from archiver.loader import load_config  # noqa: E402
from archiver.logger import logger  # noqa: E402
from archiver.metadata_schema import (  # noqa: E402
    METADATA_SCHEMA,
    METADATA_SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    metadata_table,
    to_us,
)

load_dotenv()

BACKUP_PREFIX = "_backup/metadata-v0/"
# Audit trail on rewritten partitions, next to schema_version/skipped_lines.
REWRITTEN_FROM_KEY = b"rewritten_from_version"
DUPLICATES_REMOVED_KEY = b"duplicates_removed"

_KNOWN = set(METADATA_SCHEMA.names)


def partition_version(schema: pa.Schema) -> int:
    """schema_version from a partition's key-value metadata; 0 if absent."""
    raw = (schema.metadata or {}).get(SCHEMA_VERSION_KEY)
    return int(raw) if raw is not None else 0


def conform(table: pa.Table, version: int) -> tuple[pa.Table, int, int]:
    """An old partition -> (conformed table, duplicates_removed, rows_skipped).

    Rows keep their order, which is the order read_metadata produced them
    (selected files sorted by name, so hour= before window=), and the first
    occurrence of a (to_us(timestamp), digest) key wins: the same row
    dedupe_metadata_lines would have kept. A row with no finite timestamp
    is skipped and counted, as parse_metadata_lines would skip its line.
    Anything else metadata_table rejects (a column the schema doesn't have, a
    wrong-typed value) raises.
    """
    if "timestamp" not in table.column_names:
        raise ValueError("partition has no timestamp column")
    ts_type = table.schema.field("timestamp").type
    if not (pa.types.is_floating(ts_type) or pa.types.is_integer(ts_type)):
        raise ValueError(f"timestamp is {ts_type}, expected epoch seconds")

    seen: set[tuple[int, str | None]] = set()
    rows: list[dict] = []
    duplicates = skipped = 0
    for row in table.to_pylist():
        ts = row["timestamp"]
        if ts is None or not math.isfinite(ts):
            skipped += 1
            continue
        key = (to_us(ts), row.get("digest"))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        rows.append(row)

    out = metadata_table(rows, skipped=skipped)
    meta = {
        **out.schema.metadata,
        REWRITTEN_FROM_KEY: str(version).encode(),
        DUPLICATES_REMOVED_KEY: str(duplicates).encode(),
    }
    return out.replace_schema_metadata(meta), duplicates, skipped


@dataclass
class Result:
    key: str
    size: int
    version: int = -1
    columns: dict[str, str] = field(default_factory=dict)  # name -> arrow type
    rows_before: int = 0
    rows_after: int = 0
    duplicates: int = 0
    skipped: int = 0
    written: bool = False
    error: str | None = None


class Rewriter:
    def __init__(self, bucket: str, region: str, *, apply: bool, backup: bool):
        import boto3

        self.bucket = bucket
        self.apply = apply
        self.backup = backup
        self.client = boto3.client("s3", region_name=region)
        self.fs = pafs.S3FileSystem(region=region)

    def discover(self, prefix: str, feeds: set[str] | None) -> list[tuple[str, int]]:
        """(key, size) for every metadata partition under prefix."""
        found = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/data.parquet"):
                    continue
                if feeds is not None and _feed_of(key) not in feeds:
                    continue
                found.append((key, obj["Size"]))
        return found

    def process(self, key: str, size: int, *, footers_only: bool) -> Result:
        result = Result(key=key, size=size)
        try:
            with self.fs.open_input_file(f"{self.bucket}/{key}") as f:
                pf = pq.ParquetFile(f)
                schema = pf.schema_arrow
                result.version = partition_version(schema)
                result.columns = {fld.name: str(fld.type) for fld in schema}
                result.rows_before = pf.metadata.num_rows
                if footers_only or result.version >= METADATA_SCHEMA_VERSION:
                    return result
                table = pf.read()
            out, result.duplicates, result.skipped = conform(table, result.version)
            result.rows_after = out.num_rows
            if self.apply:
                self._write(key, out)
                result.written = True
        except Exception as exc:  # reported per partition; the run carries on
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def _write(self, key: str, table: pa.Table) -> None:
        if self.backup:
            self.client.copy_object(
                Bucket=self.bucket,
                CopySource={"Bucket": self.bucket, "Key": key},
                Key=BACKUP_PREFIX + key,
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "data.parquet"
            pq.write_table(table, local)  # same writer call as Rollup._write_parquet
            tmp_key = key + ".rewriting"
            self.client.upload_file(str(local), self.bucket, tmp_key)
            self.client.copy_object(
                Bucket=self.bucket,
                CopySource={"Bucket": self.bucket, "Key": tmp_key},
                Key=key,
            )
            self.client.delete_object(Bucket=self.bucket, Key=tmp_key)

        with self.fs.open_input_file(f"{self.bucket}/{key}") as f:
            back = pq.ParquetFile(f)
            if partition_version(back.schema_arrow) != METADATA_SCHEMA_VERSION:
                raise RuntimeError("read-back is not at the current schema_version")
            if back.metadata.num_rows != table.num_rows:
                raise RuntimeError(
                    f"read-back has {back.metadata.num_rows} rows, wrote {table.num_rows}"
                )


def _feed_of(key: str) -> str:
    # [<hot_prefix>]metadata/feed=<name>/year=Y/month=M/day=D/data.parquet
    for segment in key.split("/"):
        if segment.startswith("feed="):
            return segment.removeprefix("feed=")
    return ""


def _day_of(key: str) -> str:
    parts = dict(s.split("=", 1) for s in key.split("/") if "=" in s)
    return f"{parts.get('year')}-{int(parts.get('month', 0)):02d}-{int(parts.get('day', 0)):02d}"


def report(
    results: list[Result], *, footers_only: bool, apply: bool, examples: int
) -> int:
    """Print the run summary; return the number of failed partitions."""
    total_bytes = sum(r.size for r in results)
    print(f"\n{len(results):,} partitions, {total_bytes / 1e9:.2f} GB")

    versions = Counter(r.version for r in results)  # -1: footer unreadable
    print(
        "by schema_version: "
        + ", ".join(
            f"{'unreadable' if v < 0 else f'v{v}'}: {n:,}"
            for v, n in sorted(versions.items())
        )
    )

    types: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        for name, typ in r.columns.items():
            types[name][typ] += 1
    print("\ncolumns seen (partitions per type):")
    for name in sorted(types, key=lambda n: (n not in _KNOWN, n)):
        flag = "" if name in _KNOWN else "   <-- NOT IN METADATA_SCHEMA"
        variants = ", ".join(f"{t}: {n:,}" for t, n in types[name].most_common())
        print(f"  {name:24} {variants}{flag}")

    unknown = {
        name: [r.key for r in results if name in r.columns][:examples]
        for name in types
        if name not in _KNOWN
    }
    for name, keys in unknown.items():
        print(f"\n'{name}' appears in e.g.:\n  " + "\n  ".join(keys))

    failed = [r for r in results if r.error]
    if not footers_only:
        stale = [r for r in results if 0 <= r.version < METADATA_SCHEMA_VERSION]
        converted = [r for r in stale if not r.error]
        dup_rows = sum(r.duplicates for r in converted)
        skipped = sum(r.skipped for r in converted)
        verb = "rewrote" if apply else "would rewrite"
        print(
            f"\n{verb} {len(converted):,}/{len(stale):,} stale partitions: "
            f"{sum(r.rows_before for r in converted):,} -> "
            f"{sum(r.rows_after for r in converted):,} rows "
            f"({dup_rows:,} duplicates removed, {skipped:,} rows without a timestamp)"
        )
        by_day = Counter()
        for r in converted:
            if r.duplicates:
                by_day[_day_of(r.key)] += 1
        if by_day:
            print("partitions with duplicates, by day:")
            for day, n in sorted(by_day.items()):
                print(f"  {day}: {n:,}")

    if failed:
        errors = Counter(r.error for r in failed)
        print(f"\n{len(failed):,} partitions FAILED:")
        for error, n in errors.most_common():
            keys = [r.key for r in failed if r.error == error][:examples]
            print(f"  {n:,} x {error}\n    e.g. " + "\n         ".join(keys))
    return len(failed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config/feeds.yaml"),
        help="Path to feeds.yaml (for s3.hot_bucket, hot_prefix and region).",
    )
    p.add_argument("--feed", nargs="+", default=None, help="Only these feeds.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--footers-only",
        action="store_true",
        help="Read only parquet footers: versions, columns and types. Fast.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite stale partitions in place. Without it, nothing is written.",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help=f"With --apply, skip copying each original to {BACKUP_PREFIX}<key>.",
    )
    p.add_argument("--workers", type=int, default=16, help="Concurrent partitions.")
    p.add_argument("--examples", type=int, default=3, help="Example keys per finding.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(str(args.config))
    prefix = config.s3.hot_prefix + "metadata/"
    rewriter = Rewriter(
        config.s3.hot_bucket,
        config.s3.region,
        apply=args.apply,
        backup=not args.no_backup,
    )
    keys = rewriter.discover(prefix, set(args.feed) if args.feed else None)
    print(
        f"found {len(keys):,} metadata partitions under s3://{config.s3.hot_bucket}/{prefix}",
        file=sys.stderr,
    )

    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(rewriter.process, key, size, footers_only=args.footers_only)
            for key, size in keys
        ]
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            if result.error:
                logger.error("%s: %s", result.key, result.error)
            if i % 1000 == 0:
                print(f"  {i:,}/{len(keys):,}", file=sys.stderr)

    failed = report(
        results,
        footers_only=args.footers_only,
        apply=args.apply,
        examples=args.examples,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
