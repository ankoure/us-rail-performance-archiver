"""Build the gold tier: daily performance-metrics marts from curated parquet.

For each (feed, day) this reads the silver `vehicles` (or `trip_updates`)
partition, derives Visits, folds them with `analysis.metrics.compute_marts`, and
writes two parquet marts:

    {curated}/metrics/stop_day/feed={feed}/year=/month=/day=/data.parquet
    {curated}/metrics/route_day/feed={feed}/year=/month=/day=/data.parquet

and an event-grain mart with one ARR/DEP row per stop visit, all routes in one
partition per day:

    {curated}/metrics/events/feed={feed}/year=/month=/day=/data.parquet

It mirrors rollup.py's CLI shell and ship.py's per-(feed, day) loop, so the prod
batch chain can call `python gold.py --day "$DAY"` symmetrically between rollup
and ship. Missing partitions skip rather than abort.

Examples:

    # one feed, one day
    uv run python gold.py --feed wmata-vehicles --day 2026-05-20

    # every feed in the config, every day already on disk
    uv run python gold.py --all-days

    # light-rail feeds whose vehicle pings omit stop_id
    uv run python gold.py --feed metromn-trips --source trip-updates --day 2026-05-22
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

from analysis.metrics import (
    EVENTS_SCHEMA,
    ROUTE_DAY_SCHEMA,
    STOP_DAY_SCHEMA,
    compute_events,
    compute_marts,
)
from analysis.trip_updates_day import TripUpdatesDay
from analysis.vehicle_day import VehicleDay
from archiver.loader import load_config

load_dotenv()

_PART_RE = re.compile(r"^(year|month|day)=(\d+)$")
_SOURCE_SUBDIR = {"vehicles": "vehicles", "trip-updates": "trip_updates"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed",
        nargs="+",
        default=None,
        help="One or more feed names. Omit to process every feed in the config.",
    )
    p.add_argument(
        "--day",
        type=dt.date.fromisoformat,
        help="A single day (YYYY-MM-DD) to build.",
    )
    p.add_argument(
        "--all-days",
        action="store_true",
        help="Build every service_date partition on disk for each feed "
        "(unioned with --day).",
    )
    p.add_argument(
        "--source",
        choices=["vehicles", "trip-updates"],
        default="vehicles",
        help="Which curated dataset to derive Visits from (default: vehicles).",
    )
    p.add_argument(
        "--curated-dir",
        type=Path,
        default=Path("curated"),
        help="Curated root: read silver from here and write metrics under it.",
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config/feeds.yaml"),
        help="Path to feeds.yaml (feed->timezone lookup and default feed list).",
    )
    p.add_argument(
        "--merge-gap-seconds",
        type=int,
        default=60,
        help="Collapse same-stop visits separated by <= this many seconds "
        "(default: 60). Set to 0 to disable.",
    )
    p.add_argument("-f", "--force", action="store_true", help="Overwrite existing marts.")
    args = p.parse_args(argv)
    if not args.day and not args.all_days:
        p.error("provide --day YYYY-MM-DD or --all-days")
    return args


def load_feed_tz_map(config_path: Path) -> dict[str, str]:
    """feed_name -> IANA timezone, gathered from every agency in the config."""
    config = load_config(str(config_path))
    return {
        feed.name: agency.timezone
        for agency in config.agencies
        for feed in agency.feeds
    }


def discover_dates(feed: str, curated_dir: Path, source: str) -> list[dt.date]:
    """Every service_date with a partition for (feed, source) under curated_dir."""
    feed_root = curated_dir / _SOURCE_SUBDIR[source] / f"feed={feed}"
    if not feed_root.exists():
        return []
    found: list[dt.date] = []
    for day_dir in feed_root.glob("year=*/month=*/day=*"):
        parts: dict[str, int] = {}
        for part in day_dir.parts[-3:]:
            m = _PART_RE.match(part)
            if m:
                parts[m.group(1)] = int(m.group(2))
        if set(parts) != {"year", "month", "day"}:
            continue
        try:
            found.append(dt.date(parts["year"], parts["month"], parts["day"]))
        except ValueError:
            continue
    return sorted(set(found))


def _mart_path(curated_dir: Path, mart: str, feed: str, day: dt.date) -> Path:
    return (
        curated_dir
        / "metrics"
        / mart
        / f"feed={feed}"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
        / "data.parquet"
    )


def _write_parquet(rows: list[dict], schema: pa.Schema, path: Path) -> None:
    """Atomic write via tmp + rename (mirrors Rollup._write_parquet)."""
    table = pa.Table.from_pylist(rows, schema=schema)
    tmp = path.with_suffix(".parquet.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, tmp)
    tmp.rename(path)


def build_one(
    feed: str,
    day: dt.date,
    tz: ZoneInfo,
    curated_dir: Path,
    source: str,
    merge_gap_seconds: int,
    force: bool,
) -> tuple[int, int, int] | None:
    """Build all marts for one (feed, day). Returns (stop, route, event) counts.

    Returns None when there's nothing to do (already built without --force, or
    no partition / no visits on disk).
    """
    stop_path = _mart_path(curated_dir, "stop_day", feed, day)
    route_path = _mart_path(curated_dir, "route_day", feed, day)
    events_path = _mart_path(curated_dir, "events", feed, day)
    if (
        not force
        and stop_path.exists()
        and route_path.exists()
        and events_path.exists()
    ):
        print(f"[{feed} {day}] exists — skipping (use --force)", file=sys.stderr)
        return None

    day_cls = VehicleDay if source == "vehicles" else TripUpdatesDay
    try:
        vd = day_cls(feed, day, base_dir=curated_dir, merge_gap_seconds=merge_gap_seconds)
        visits = [v for veh in vd.vehicles for v in veh.dwells]
    except FileNotFoundError as e:
        print(f"[{feed} {day}] SKIP — {e}", file=sys.stderr)
        return None

    stop_rows, route_rows = compute_marts(visits, feed, tz)
    event_rows = compute_events(visits, feed, tz)
    if not stop_rows and not route_rows and not event_rows:
        print(f"[{feed} {day}] no metrics rows — skipping", file=sys.stderr)
        return None

    _write_parquet(stop_rows, STOP_DAY_SCHEMA, stop_path)
    _write_parquet(route_rows, ROUTE_DAY_SCHEMA, route_path)
    _write_parquet(event_rows, EVENTS_SCHEMA, events_path)
    print(
        f"[{feed} {day}] {len(stop_rows):>7,} stop-day  "
        f"{len(route_rows):>6,} route-day  {len(event_rows):>8,} events"
    )
    return len(stop_rows), len(route_rows), len(event_rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    feed_tz_map = load_feed_tz_map(args.config)
    feeds = args.feed if args.feed else sorted(feed_tz_map)
    base_dates = [args.day] if args.day else []

    grand_stop = 0
    grand_route = 0
    grand_events = 0
    for feed in feeds:
        tz_str = feed_tz_map.get(feed)
        if tz_str is None:
            print(f"[{feed}] not in {args.config} — skipping", file=sys.stderr)
            continue
        tz = ZoneInfo(tz_str)
        dates = set(base_dates)
        if args.all_days:
            dates |= set(discover_dates(feed, args.curated_dir, args.source))
        if not dates:
            print(f"[{feed}] no dates to process — skipping", file=sys.stderr)
            continue
        for day in sorted(dates):
            result = build_one(
                feed,
                day,
                tz,
                args.curated_dir,
                args.source,
                args.merge_gap_seconds,
                args.force,
            )
            if result is not None:
                grand_stop += result[0]
                grand_route += result[1]
                grand_events += result[2]

    print(
        f"---\ntotal: {grand_stop:,} stop-day rows, "
        f"{grand_route:,} route-day rows, {grand_events:,} events"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
