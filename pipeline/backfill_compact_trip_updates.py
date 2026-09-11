"""Retroactively compact every trip_updates partition already sitting in the
hot bucket, in place.

pipeline/compact_trip_updates.py (now wired into agency_batch.py's rollup
step, see its own docstring) only touches a partition the moment it's built,
on local disk, before it's shipped. Every day landed BEFORE that wiring
existed is still sitting in S3 at full poll-level fidelity. This is the
one-time catch-up for that backlog.

Confirmed against two real, already-shipped partitions (bkk-trips and
metromn-trips, 2026-09-09): 99.6% smaller (2.7 GB -> 11 MB combined), same
row-selection logic compact_trip_updates.py already uses, so the result is
byte-for-byte what the live pipeline would have shipped had this run from
day one.

Each object is downloaded, compacted, and (only if smaller) written back via
upload-to-temp-key + server-side copy-over + delete-temp -- never a direct
overwrite -- so a crash mid-upload can't leave the live object half-written.
compact_trip_updates.py's own compact_one() gets this same guarantee for free
from a local filesystem rename; S3 has no rename, so this is the equivalent.

Every object is handled in its own subprocess (--one), matching
agency_batch.py's per-agency subprocess isolation: pyarrow/pandas memory from
a 1+ GB parquet file doesn't reliably return to the allocator between
iterations in one long-lived process (see compact_parquet's own docstring for
the file this bit the backfill task on), and this script may touch tens of
thousands of objects in one run. --workers fans those subprocesses out
(matching agency_batch.py --workers) since each one is I/O-bound during its
download/upload and this is a one-time job, not the nightly hot path.

Examples:

    # see how many partitions exist and how big the job is, without changing anything
    uv run python pipeline/backfill_compact_trip_updates.py -c config/feeds.yaml --dry-run

    # one feed at a time, for a staged rollout
    uv run python pipeline/backfill_compact_trip_updates.py -c config/feeds.yaml --feed bkk-trips

    # everything, fanned out
    uv run python pipeline/backfill_compact_trip_updates.py -c config/feeds.yaml --workers 8

    # internal: compact exactly one already-known key (used by the parent process)
    uv run python pipeline/backfill_compact_trip_updates.py -c config/feeds.yaml --one trip_updates/feed=bkk-trips/year=2026/month=9/day=9/data.parquet
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Make the repo root importable when run as `python pipeline/backfill_compact_trip_updates.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow.parquet as pq
from dotenv import load_dotenv

from archiver.loader import load_config  # noqa: E402
from archiver.logger import logger  # noqa: E402
from pipeline.compact_trip_updates import compact_parquet  # noqa: E402

load_dotenv()

_PREFIX = "trip_updates/"


def discover_keys(client, bucket: str, feeds: list[str] | None) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/data.parquet"):
                continue
            if feeds is not None:
                # key shape: trip_updates/feed=<name>/year=Y/month=M/day=D/data.parquet
                feed_segment = key.split("/", 2)[1]
                if feed_segment.removeprefix("feed=") not in feeds:
                    continue
            keys.append(key)
    return keys


def compact_one_key(client, bucket: str, key: str) -> tuple[int, int, int, int] | None:
    """Download, compact, and (if smaller) overwrite one S3 object in place.

    Returns (rows_before, rows_after, bytes_before, bytes_after), or None if
    there was nothing to compact (empty, or already minimal).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = Path(tmpdir) / "data.parquet"
        client.download_file(bucket, key, str(local_path))
        bytes_before = local_path.stat().st_size

        pf = pq.ParquetFile(local_path)
        if pf.metadata.num_rows == 0:
            return None
        rows_before, compacted = compact_parquet(pf)
        if compacted.num_rows == rows_before:
            return None

        out_path = Path(tmpdir) / "compacted.parquet"
        pq.write_table(compacted, out_path)
        bytes_after = out_path.stat().st_size
        # Safety net: pyarrow's encoding on a tiny table is occasionally not
        # smaller than the original despite fewer rows (dictionary/metadata
        # overhead dominating) -- never write back something that isn't an
        # actual improvement.
        if bytes_after >= bytes_before:
            return None

        tmp_key = key + ".compacting"
        client.upload_file(str(out_path), bucket, tmp_key)
        client.copy_object(
            Bucket=bucket, CopySource={"Bucket": bucket, "Key": tmp_key}, Key=key
        )
        client.delete_object(Bucket=bucket, Key=tmp_key)
        return (rows_before, compacted.num_rows, bytes_before, bytes_after)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config/feeds.yaml"),
        help="Path to feeds.yaml (feed->timezone lookup and s3.hot_bucket).",
    )
    p.add_argument(
        "--feed",
        nargs="+",
        default=None,
        help="One or more feed names. Omit to process every feed with a "
        "trip_updates partition in the hot bucket.",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Concurrent per-object subprocesses (default: 2). Measured peak "
        "for the single heaviest known feed (bkk-trips) is ~7.2 GB RSS per "
        "object -- keep this conservative unless the task's memory is sized "
        "to cover workers x that peak simultaneously.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching partitions and exit without downloading or "
        "changing anything.",
    )
    p.add_argument(
        "--one",
        default=None,
        help=argparse.SUPPRESS,  # internal: compact exactly one key, used by the parent process
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(str(args.config))
    bucket = config.s3.hot_bucket

    import boto3

    client = boto3.client("s3", region_name=config.s3.region)

    if args.one:
        result = compact_one_key(client, bucket, args.one)
        if result:
            rows_before, rows_after, bytes_before, bytes_after = result
            pct = 100 * (1 - bytes_after / bytes_before)
            print(
                f"{args.one}: {rows_before:,}->{rows_after:,} rows, "
                f"{bytes_before:,}->{bytes_after:,} bytes ({pct:.1f}% smaller)"
            )
        return 0

    keys = discover_keys(client, bucket, args.feed)
    print(f"found {len(keys)} trip_updates partitions to check", file=sys.stderr)
    if args.dry_run or not keys:
        return 0

    # S3 lists keys lexicographically, so one heavy feed's ~dozens of day
    # partitions (e.g. feed=bkk-trips/...) land contiguously -- submitting in
    # that order risks several of that feed's ~7.2 GB-peak objects landing on
    # concurrent workers at once. Shuffling spreads same-feed (same-size-class)
    # objects across the run instead of clustering them.
    random.shuffle(keys)

    compacted_count = 0
    total_before = total_after = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                subprocess.run,
                [sys.executable, __file__, "-c", str(args.config), "--one", key],
                capture_output=True,
                text=True,
            ): key
            for key in keys
        }
        for i, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            result = future.result()
            if result.returncode != 0:
                logger.error(
                    "[%d/%d] failed on %s: %s",
                    i,
                    len(keys),
                    key,
                    result.stderr.strip()[-500:],
                )
                continue
            line = result.stdout.strip()
            if line:
                compacted_count += 1
                print(f"[{i}/{len(keys)}] {line}")
                try:
                    bytes_part = line.split(",")[1].strip().split(" ")[0]
                    before_b, after_b = (int(x) for x in bytes_part.split("->"))
                    total_before += before_b
                    total_after += after_b
                except (IndexError, ValueError):
                    pass

    print(
        f"---\ncompacted {compacted_count}/{len(keys)} partitions"
        + (
            f", {total_before:,} -> {total_after:,} bytes "
            f"({100 * (1 - total_after / total_before):.1f}% smaller)"
            if total_before
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
