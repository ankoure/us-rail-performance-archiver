"""Build the gold tier: daily performance-metrics marts from curated parquet.

For each (feed, day) this reads the silver `vehicles` (or `trip_updates`)
partition, derives Visits, folds them with `analysis.metrics.compute_marts`, and
writes two parquet marts:

    {curated}/metrics/stop_day/feed={feed}/year=/month=/day=/data.parquet
    {curated}/metrics/route_day/feed={feed}/year=/month=/day=/data.parquet

and an event-grain mart with one ARR/DEP row per stop visit, all routes in one
partition per day:

    {curated}/metrics/events/feed={feed}/year=/month=/day=/data.parquet

When the feed's agency declares an `mdb_feed_id`, it also joins each visit to the
static GTFS schedule (`analysis.adherence`) and writes the on-time-performance
marts — a trip-stop fact table plus stop-day / route-day rollups:

    {curated}/metrics/adherence/feed={feed}/year=/month=/day=/data.parquet
    {curated}/metrics/stop_day_otp/...
    {curated}/metrics/route_day_otp/...

OTP runs in the prod daily batch (pandas is a runtime dependency). It is still
best-effort: it self-skips when an agency has no mdb_feed_id, when the schedule
snapshot can't be resolved, or — defensively — when pandas is unavailable, and
none of these block the schedule-free marts. Pass --no-otp to skip it explicitly.

It mirrors rollup.py's CLI shell and ship.py's per-(feed, day) loop, so the prod
batch chain can call `python gold.py --day "$DAY"` symmetrically between rollup
and ship. Missing partitions skip rather than abort.

Examples:

    # one feed, one day (schedule-free marts + OTP, GTFS auto-resolved)
    uv run python gold.py --feed wmata-vehicles --day 2026-05-20

    # every feed in the config, every day already on disk
    uv run python gold.py --all-days

    # light-rail feeds whose vehicle pings omit stop_id
    uv run python gold.py --feed metromn-trips --source trip-updates --day 2026-05-22

    # schedule-free marts only, with a stricter on-time window
    uv run python gold.py --all-days --no-otp
    uv run python gold.py --feed metra-vehicles --day 2026-05-20 --late-threshold-seconds 360
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

_SCHEDULE_FREE_MARTS = ("stop_day", "route_day", "events")
_OTP_MARTS = ("adherence", "stop_day_otp", "route_day_otp")

# GTFS resolver defaults — duplicated from analysis.gtfs_fetcher rather than
# imported, so gold.py's module import stays pandas-free. The on-time-performance
# path imports gtfs_fetcher (pandas-backed) lazily in main(); see that module for
# the canonical values and keep these in sync.
_DEFAULT_GTFS_API_URL = "https://mwue7uiyf5.execute-api.us-east-1.amazonaws.com/api"
_DEFAULT_GTFS_CACHE_DIR = Path("static_gtfs")


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
    p.add_argument(
        "--no-otp",
        action="store_true",
        help="Skip the on-time-performance (schedule-adherence) marts; build only "
        "the schedule-free marts. OTP also self-skips when a feed's agency has no "
        "mdb_feed_id or the schedule can't be resolved.",
    )
    p.add_argument(
        "--early-threshold-seconds",
        type=int,
        default=60,
        help="A visit arriving more than this many seconds early is 'early' "
        "(default: 60).",
    )
    p.add_argument(
        "--late-threshold-seconds",
        type=int,
        default=300,
        help="A visit arriving more than this many seconds late is 'late' "
        "(default: 300). Between the two thresholds is 'on_time'.",
    )
    p.add_argument(
        "--gtfs-cache-dir",
        type=Path,
        default=_DEFAULT_GTFS_CACHE_DIR,
        help=f"Where fetched static GTFS zips are cached (default: "
        f"{_DEFAULT_GTFS_CACHE_DIR}).",
    )
    p.add_argument(
        "--gtfs-api-url",
        default=_DEFAULT_GTFS_API_URL,
        help="Archived-feeds catalog base URL for resolving GTFS snapshots.",
    )
    p.add_argument(
        "-f", "--force", action="store_true", help="Overwrite existing marts."
    )
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


def load_feed_agency_map(config_path: Path) -> dict[str, tuple[str, str | None]]:
    """feed_name -> (agency_id, mdb_feed_id_or_None) for GTFS-snapshot resolution.

    Mirrors scripts/export_events.py: a feed's parent agency supplies both the
    cache-path slug (agency_id) and the archived-feeds catalog id (mdb_feed_id).
    """
    config = load_config(str(config_path))
    return {
        feed.name: (agency.agency_id, agency.mdb_feed_id)
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
    *,
    gtfs_for=None,
    early_threshold_s: int = 60,
    late_threshold_s: int = 300,
) -> dict | None:
    """Build the marts for one (feed, day). Returns a counts dict, or None.

    Two independent, separately-idempotent steps off a single Visit load: the
    schedule-free marts (stop_day / route_day / events) and — when `gtfs_for` is
    supplied — the on-time-performance marts (adherence / stop_day_otp /
    route_day_otp). Returns None when there's nothing to do (all marts already
    built without --force, or no partition on disk).

    `gtfs_for(feed, day) -> StaticGtfs | None` resolves the schedule snapshot; it
    is None when OTP is disabled. GTFS lookup failures skip only the OTP step.
    """
    sf_paths = {m: _mart_path(curated_dir, m, feed, day) for m in _SCHEDULE_FREE_MARTS}
    otp_paths = {m: _mart_path(curated_dir, m, feed, day) for m in _OTP_MARTS}
    need_sf = force or not all(p.exists() for p in sf_paths.values())
    need_otp = gtfs_for is not None and (
        force or not all(p.exists() for p in otp_paths.values())
    )
    if not need_sf and not need_otp:
        print(f"[{feed} {day}] exists — skipping (use --force)", file=sys.stderr)
        return None

    day_cls = VehicleDay if source == "vehicles" else TripUpdatesDay
    try:
        vd = day_cls(
            feed, day, base_dir=curated_dir, merge_gap_seconds=merge_gap_seconds
        )
        visits = [v for veh in vd.vehicles for v in veh.dwells]
    except FileNotFoundError as e:
        print(f"[{feed} {day}] SKIP — {e}", file=sys.stderr)
        return None

    result = {"stop": 0, "route": 0, "events": 0, "adherence": 0, "on_time": 0}
    if need_sf:
        stop_rows, route_rows = compute_marts(visits, feed, tz)
        event_rows = compute_events(visits, feed, tz)
        if stop_rows or route_rows or event_rows:
            _write_parquet(stop_rows, STOP_DAY_SCHEMA, sf_paths["stop_day"])
            _write_parquet(route_rows, ROUTE_DAY_SCHEMA, sf_paths["route_day"])
            _write_parquet(event_rows, EVENTS_SCHEMA, sf_paths["events"])
            result.update(
                stop=len(stop_rows), route=len(route_rows), events=len(event_rows)
            )
            print(
                f"[{feed} {day}] {len(stop_rows):>7,} stop-day  "
                f"{len(route_rows):>6,} route-day  {len(event_rows):>8,} events"
            )
        else:
            print(f"[{feed} {day}] no metrics rows", file=sys.stderr)

    if need_otp:
        otp = _build_otp(
            feed,
            day,
            tz,
            visits,
            gtfs_for,
            otp_paths,
            early_threshold_s,
            late_threshold_s,
        )
        if otp is not None:
            result.update(adherence=otp[0], on_time=otp[1])

    return result


def _build_otp(
    feed: str,
    day: dt.date,
    tz: ZoneInfo,
    visits: list,
    gtfs_for,
    otp_paths: dict[str, Path],
    early_threshold_s: int,
    late_threshold_s: int,
) -> tuple[int, int] | None:
    """Build the OTP marts for one (feed, day). Returns (matched, on_time) or None.

    Imports the pandas-backed GTFS path lazily so gold.py stays importable (and
    the schedule-free marts keep building) where pandas is absent. Resolves the
    schedule for `day` plus the prior day (owl trips), folds adherence, and writes
    the three OTP marts. Any GTFS lookup failure skips OTP for this (feed, day).
    """
    from analysis.adherence import (
        ADHERENCE_SCHEMA,
        ROUTE_DAY_OTP_SCHEMA,
        STOP_DAY_OTP_SCHEMA,
        compute_adherence,
        schedule_index,
    )

    try:
        gtfs_day = gtfs_for(feed, day)
    except (LookupError, FileNotFoundError) as e:
        print(f"[{feed} {day}] SKIP OTP — GTFS lookup: {e}", file=sys.stderr)
        return None
    if gtfs_day is None:
        print(
            f"[{feed} {day}] no mdb_feed_id for this agency — skipping OTP",
            file=sys.stderr,
        )
        return None

    schedules = {day: schedule_index(gtfs_day, day)}
    prev = day - dt.timedelta(days=1)
    try:  # prior-day schedule backs owl trips; best-effort.
        gtfs_prev = gtfs_for(feed, prev)
        if gtfs_prev is not None:
            schedules[prev] = schedule_index(gtfs_prev, prev)
    except (LookupError, FileNotFoundError):
        pass

    fact, stop_rows, route_rows = compute_adherence(
        visits,
        schedules,
        feed,
        tz,
        early_threshold_s=early_threshold_s,
        late_threshold_s=late_threshold_s,
        route_modes=gtfs_day.route_modes,
    )
    if not fact:
        print(f"[{feed} {day}] no schedule matches — skipping OTP", file=sys.stderr)
        return None

    _write_parquet(fact, ADHERENCE_SCHEMA, otp_paths["adherence"])
    _write_parquet(stop_rows, STOP_DAY_OTP_SCHEMA, otp_paths["stop_day_otp"])
    _write_parquet(route_rows, ROUTE_DAY_OTP_SCHEMA, otp_paths["route_day_otp"])

    matched = len(fact)
    on_time = sum(1 for r in fact if r["on_time"])
    candidates = sum(1 for v in visits if v.trip_id)
    pct = 100 * on_time / matched if matched else 0.0
    rate = 100 * matched / candidates if candidates else 0.0
    print(
        f"[{feed} {day}] {matched:>8,} adherence  {pct:5.1f}% on-time  "
        f"({rate:4.1f}% of {candidates:,} trip-visits matched schedule)"
    )
    return matched, on_time


def _make_gtfs_resolver(args: argparse.Namespace):
    """Build a `gtfs_for(feed, day) -> StaticGtfs | None` resolver, or None.

    Returns None — with a one-line notice — when pandas isn't installed, so the
    prod batch (which ships without pandas) builds the schedule-free marts and
    silently skips OTP. Otherwise yields a per-agency-cached resolver keyed off
    each agency's mdb_feed_id, mirroring scripts/export_events.py.
    """
    try:
        from analysis.gtfs_fetcher import GtfsResolver
    except ImportError as e:
        print(
            f"[otp] disabled — {e}. Install dev deps (pandas) to build OTP marts.",
            file=sys.stderr,
        )
        return None

    feed_agency_map = load_feed_agency_map(args.config)
    cache: dict[str, GtfsResolver] = {}

    def gtfs_for(feed: str, day: dt.date):
        info = feed_agency_map.get(feed)
        if info is None or not info[1]:
            return None
        agency_id, mdb_id = info
        if agency_id not in cache:
            cache[agency_id] = GtfsResolver(
                mdb_id,
                agency_id.lower(),
                cache_dir=args.gtfs_cache_dir,
                api_url=args.gtfs_api_url,
            )
        return cache[agency_id].for_date(day)

    return gtfs_for


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    feed_tz_map = load_feed_tz_map(args.config)
    feeds = args.feed if args.feed else sorted(feed_tz_map)
    base_dates = [args.day] if args.day else []
    gtfs_for = None if args.no_otp else _make_gtfs_resolver(args)

    totals = {"stop": 0, "route": 0, "events": 0, "adherence": 0, "on_time": 0}
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
                gtfs_for=gtfs_for,
                early_threshold_s=args.early_threshold_seconds,
                late_threshold_s=args.late_threshold_seconds,
            )
            if result is not None:
                for key in totals:
                    totals[key] += result[key]

    summary = (
        f"---\ntotal: {totals['stop']:,} stop-day rows, "
        f"{totals['route']:,} route-day rows, {totals['events']:,} events"
    )
    if gtfs_for is not None:
        otp_pct = (
            100 * totals["on_time"] / totals["adherence"]
            if totals["adherence"]
            else 0.0
        )
        summary += f", {totals['adherence']:,} adherence rows ({otp_pct:.1f}% on-time)"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
