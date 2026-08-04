"""Compact curated trip_updates partitions to one row per stop visit.

Every current consumer of trip_updates (analysis.trip_updates_day.TripUpdatesDay,
scripts/export_events.py) already reduces a (feed, day) partition down to one row
per (trip_id, stop_id) — the prediction with the latest feed_timestamp — before
using it. The curated parquet itself still carries every poll, so this rewrites
each partition to exactly what's actually used, in place.

This is a storage optimization only: nothing about trip prediction correctness
changes, since gold.py's marts are already built from the same reduction (see
TripUpdatesDay._dedupe_latest_per_key). Poll-level history isn't lost either —
the raw landing bins are archived to cold storage (DEEP_ARCHIVE) independently
of curated trip_updates, so a full re-roll from cold recovers it if ever needed.

Meant to run after gold.py (which needs full-fidelity trip_updates to build
marts) and before ship.py (so ship uploads the already-compacted file, not the
uncompacted one).

Examples:

    # one feed, one day
    uv run python pipeline/compact_trip_updates.py --feed metromn-trips --day 2026-08-01

    # every trip_updates partition on disk
    uv run python pipeline/compact_trip_updates.py --all-days
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from dotenv import load_dotenv

# Make the repo root importable when run as `python pipeline/compact_trip_updates.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.logger import logger  # noqa: E402

load_dotenv()

_TRIP_ID_COL = "trip_update.trip.trip_id"
_STOP_ID_COL = "trip_update.stop_time_update.stop_id"
_KEY_COLS = (_TRIP_ID_COL, _STOP_ID_COL)
_TIME_COL = "feed_timestamp"
_SCHED_REL_COL = "trip_update.stop_time_update.schedule_relationship"
_ARRIVAL_TIME_COL = "trip_update.stop_time_update.arrival.time"
_DEPARTURE_TIME_COL = "trip_update.stop_time_update.departure.time"


def compact_table(table: pa.Table) -> pa.Table:
    """Keep one row per (trip_id, stop_id): the row with the latest feed_timestamp.

    Mirrors TripUpdatesDay._dedupe_latest_per_key's reduction exactly — same
    junk-row filter (missing key/timestamp, SKIPPED/NO_DATA schedule
    relationship, neither arrival nor departure time) and the same latest-wins
    selection — just over the full curated schema rather than that method's
    internal column subset, so the file on disk becomes precisely what every
    consumer already reduces it to. Getting the junk filter right matters here:
    without it, a stop whose *latest* poll happens to be junk would win over an
    earlier real prediction, which is the opposite of what TripUpdatesDay
    actually selects.
    """
    if table.num_rows == 0:
        return table

    mask = pc.and_(pc.is_valid(table[_TRIP_ID_COL]), pc.is_valid(table[_STOP_ID_COL]))
    mask = pc.and_(mask, pc.is_valid(table[_TIME_COL]))
    # Treat null schedule_relationship as SCHEDULED (per GTFS-RT spec): fill_null
    # collapses arrow's 3-valued bool to 2-valued so the AND below doesn't drop
    # the row when the equality returns null on a null input.
    sched_ok = pc.fill_null(pc.equal(table[_SCHED_REL_COL], "SCHEDULED"), True)
    mask = pc.and_(mask, sched_ok)
    has_time = pc.or_(
        pc.is_valid(table[_ARRIVAL_TIME_COL]), pc.is_valid(table[_DEPARTURE_TIME_COL])
    )
    mask = pc.and_(mask, has_time)
    filtered = table.filter(mask)
    if filtered.num_rows == 0:
        return filtered

    sorted_tbl = filtered.sort_by([(_TIME_COL, "descending")])
    val_cols = [c for c in sorted_tbl.column_names if c not in _KEY_COLS]
    # use_threads=False: group_by's 'first' is an ordered aggregator and refuses
    # to run multi-threaded (same constraint as TripUpdatesDay's own dedup).
    deduped = sorted_tbl.group_by(list(_KEY_COLS), use_threads=False).aggregate(
        [(c, "first") for c in val_cols]
    )
    # 'first' aggregation suffixes value columns with '_first' — strip it back.
    new_names = [
        c if c in _KEY_COLS else c[: -len("_first")] for c in deduped.column_names
    ]
    return deduped.rename_columns(new_names).select(table.column_names)


def compact_parquet(
    pf: pq.ParquetFile, batch_rows: int = 2_000_000
) -> tuple[int, pa.Table]:
    """Batched reduction: accumulate row groups until ~batch_rows, reduce that
    batch, repeat, then one final combine pass over the (much smaller)
    partials — bounding peak memory without the whole file ever being
    materialized at once. metromn-trips' biggest day is 70M+ rows across
    3,144 row groups (~23K rows each); materializing the whole table and
    running sort+group_by over it in one shot is what OOM-killed the backfill
    task even at low concurrency. But reducing every row group individually
    is worse, not better — 3,144 separate sort+hash-aggregate calls leaves
    pyarrow's memory pool holding far more than any single batch's own size,
    since it doesn't cleanly return memory between many tiny operations.
    Batching into ~30-40 calls instead of 3,144 avoids that overhead while
    still keeping any single batch a small fraction of the full file.

    Returns (total rows read, compacted table).
    """
    before_rows = pf.metadata.num_rows
    partials: list[pa.Table] = []
    batch: list[pa.Table] = []
    batch_rows_seen = 0

    def flush_batch() -> None:
        nonlocal batch, batch_rows_seen
        if not batch:
            return
        reduced = compact_table(pa.concat_tables(batch, promote_options="default"))
        if reduced.num_rows > 0:
            partials.append(reduced)
        batch = []
        batch_rows_seen = 0

    for i in range(pf.num_row_groups):
        rg = pf.read_row_group(i)
        batch.append(rg)
        batch_rows_seen += rg.num_rows
        if batch_rows_seen >= batch_rows:
            flush_batch()
    flush_batch()

    if not partials:
        return before_rows, pf.schema_arrow.empty_table()
    combined = pa.concat_tables(partials, promote_options="default")
    return before_rows, compact_table(combined)


def _partition_path(curated_dir: Path, feed: str, day: dt.date) -> Path:
    return (
        curated_dir
        / "trip_updates"
        / f"feed={feed}"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
        / "data.parquet"
    )


def discover_dates(curated_dir: Path, feed: str) -> list[dt.date]:
    feed_root = curated_dir / "trip_updates" / f"feed={feed}"
    if not feed_root.exists():
        return []
    dates = []
    for day_dir in feed_root.glob("year=*/month=*/day=*"):
        year, month, day = (int(p.split("=")[1]) for p in day_dir.parts[-3:])
        try:
            dates.append(dt.date(year, month, day))
        except ValueError:
            continue
    return sorted(dates)


def discover_feeds(curated_dir: Path) -> list[str]:
    root = curated_dir / "trip_updates"
    if not root.exists():
        return []
    return sorted(
        p.name.removeprefix("feed=") for p in root.glob("feed=*") if p.is_dir()
    )


def compact_one(feed: str, day: dt.date, curated_dir: Path) -> tuple[int, int] | None:
    """Compact one (feed, day) partition in place. Returns (before, after) row
    counts, or None if there's no partition or nothing to do."""
    path = _partition_path(curated_dir, feed, day)
    if not path.exists():
        return None
    # ParquetFile, not pq.read_table(path): the latter auto-detects Hive-style
    # partitioning from the feed=/year=/month=/day= path segments and
    # silently injects those as extra dictionary-encoded columns, which both
    # corrupts the output schema and breaks the hash aggregation (no
    # first/last kernel for dictionary types).
    pf = pq.ParquetFile(path)
    if pf.metadata.num_rows == 0:
        return None
    before_rows, after = compact_parquet(pf)
    if after.num_rows == before_rows:
        return (before_rows, after.num_rows)

    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(after, tmp)
    tmp.rename(path)
    return (before_rows, after.num_rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed",
        nargs="+",
        default=None,
        help="One or more feed names. Omit to process every feed with a "
        "trip_updates partition on disk.",
    )
    p.add_argument(
        "--day",
        type=dt.date.fromisoformat,
        help="A single day (YYYY-MM-DD) to compact.",
    )
    p.add_argument(
        "--all-days",
        action="store_true",
        help="Compact every trip_updates partition on disk for each feed "
        "(unioned with --day).",
    )
    p.add_argument(
        "--curated-dir",
        type=Path,
        default=Path("data/curated"),
        help="Curated root (default: data/curated).",
    )
    args = p.parse_args(argv)
    if not args.day and not args.all_days:
        p.error("provide --day YYYY-MM-DD or --all-days")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    feeds = args.feed if args.feed else discover_feeds(args.curated_dir)
    if not feeds:
        print("no trip_updates partitions found", file=sys.stderr)
        return 0

    total_before = total_after = 0
    for feed in feeds:
        dates = set([args.day]) if args.day else set()
        if args.all_days:
            dates |= set(discover_dates(args.curated_dir, feed))
        if not dates:
            continue
        for day in sorted(dates):
            result = compact_one(feed, day, args.curated_dir)
            if result is None:
                continue
            before, after = result
            total_before += before
            total_after += after
            if after < before:
                pct = 100 * (1 - after / before)
                logger.info(
                    "[%s %s] %s -> %s rows (%.1f%% smaller)",
                    feed,
                    day,
                    f"{before:,}",
                    f"{after:,}",
                    pct,
                )

    if total_before:
        pct = 100 * (1 - total_after / total_before)
        print(
            f"---\ntotal: {total_before:,} -> {total_after:,} rows ({pct:.1f}% smaller)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
