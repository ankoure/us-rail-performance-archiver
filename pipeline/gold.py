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
_SPEED_MARTS = ("segment_speed", "segment_day")

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
    p.add_argument(
        "--normalize",
        action="store_true",
        help="Instead of building metrics marts, normalize the JSON-feed curated "
        "tables into curated/normalized_vehicles (resolve raw ids -> GTFS ids, "
        "standardize units/time).",
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


def discover_dates_for_dir(feed: str, root: Path) -> list[dt.date]:
    """Every service_date partition under `root/feed={feed}` (any curated table)."""
    feed_root = root / f"feed={feed}"
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


def discover_dates(feed: str, curated_dir: Path, source: str) -> list[dt.date]:
    """Every service_date with a partition for (feed, source) under curated_dir."""
    return discover_dates_for_dir(feed, curated_dir / _SOURCE_SUBDIR[source])


def _partition_path(root: Path, feed: str, day: dt.date) -> Path:
    """`<root>/feed={feed}/year=/month=/day=/data.parquet` — the curated layout.

    The single source of truth for the partition path shape; each caller supplies
    its own `<root>` (table dir, metrics mart, normalized_vehicles, ...).
    """
    return (
        root
        / f"feed={feed}"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
        / "data.parquet"
    )


def _mart_path(curated_dir: Path, mart: str, feed: str, day: dt.date) -> Path:
    return _partition_path(curated_dir / "metrics" / mart, feed, day)


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

    Three independent, separately-idempotent steps off a single Visit load: the
    schedule-free marts (stop_day / route_day / events), the on-time-performance
    marts (adherence / stop_day_otp / route_day_otp), and the segment-speed marts
    (segment_speed / segment_day). Both GTFS-gated steps share the same resolver.
    Returns None when there's nothing to do (all marts already built without
    --force, or no partition on disk).

    `gtfs_for(feed, day) -> StaticGtfs | None` resolves the schedule snapshot; it
    is None when OTP is disabled. GTFS lookup failures skip only the OTP/speed steps.
    """
    sf_paths = {m: _mart_path(curated_dir, m, feed, day) for m in _SCHEDULE_FREE_MARTS}
    otp_paths = {m: _mart_path(curated_dir, m, feed, day) for m in _OTP_MARTS}
    speed_paths = {m: _mart_path(curated_dir, m, feed, day) for m in _SPEED_MARTS}
    need_sf = force or not all(p.exists() for p in sf_paths.values())
    need_otp = gtfs_for is not None and (
        force or not all(p.exists() for p in otp_paths.values())
    )
    need_speed = gtfs_for is not None and (
        force or not all(p.exists() for p in speed_paths.values())
    )
    if not need_sf and not need_otp and not need_speed:
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

    result = {
        "stop": 0,
        "route": 0,
        "events": 0,
        "adherence": 0,
        "on_time": 0,
        "segments": 0,
    }
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

    if need_speed:
        n = _build_speed(feed, day, tz, visits, gtfs_for, speed_paths)
        if n is not None:
            result["segments"] = n

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


def _build_speed(
    feed: str,
    day: dt.date,
    tz: ZoneInfo,
    visits: list,
    gtfs_for,
    speed_paths: dict[str, Path],
) -> int | None:
    """Build the segment-speed marts for one (feed, day). Returns fact row count or None.

    Resolves the same GTFS snapshot as OTP (so no extra network I/O when both run
    together) and passes stop coordinates to compute_segment_speeds. Skips silently
    when the feed has no mdb_feed_id, the GTFS lookup fails, or the zip carries no
    stop coordinates.
    """
    from analysis.segment_speed import (
        SEGMENT_DAY_SCHEMA,
        SEGMENT_SPEED_SCHEMA,
        compute_segment_speeds,
    )

    try:
        gtfs_day = gtfs_for(feed, day)
    except (LookupError, FileNotFoundError) as e:
        print(f"[{feed} {day}] SKIP speed — GTFS lookup: {e}", file=sys.stderr)
        return None
    if gtfs_day is None:
        return None

    stop_coords = gtfs_day.stop_coords
    if not stop_coords:
        print(
            f"[{feed} {day}] SKIP speed — no stop coordinates in GTFS", file=sys.stderr
        )
        return None

    fact_rows, seg_day_rows = compute_segment_speeds(visits, stop_coords, feed, tz)
    if not fact_rows:
        print(f"[{feed} {day}] no segment speed rows", file=sys.stderr)
        return None

    _write_parquet(fact_rows, SEGMENT_SPEED_SCHEMA, speed_paths["segment_speed"])
    _write_parquet(seg_day_rows, SEGMENT_DAY_SCHEMA, speed_paths["segment_day"])
    print(
        f"[{feed} {day}] {len(fact_rows):>8,} segment-speed  "
        f"{len(seg_day_rows):>7,} segment-day"
    )
    return len(fact_rows)


def _make_gtfs_resolver(args: argparse.Namespace):
    """Build a `gtfs_for(feed, day) -> StaticGtfs | None` resolver, or None.

    Returns None — with a one-line notice — when pandas isn't installed, so the
    prod batch (which ships without pandas) builds the schedule-free marts and
    silently skips OTP. Otherwise yields a per-agency-cached resolver keyed off
    each agency's mdb_feed_id, mirroring scripts/export_events.py.
    """
    try:
        import requests

        from analysis.gtfs_fetcher import GtfsResolver
    except ImportError as e:
        print(
            f"[otp] disabled — {e}. Install dev deps (pandas) to build OTP marts.",
            file=sys.stderr,
        )
        return None

    feed_agency_map = load_feed_agency_map(args.config)
    cache: dict[str, GtfsResolver] = {}
    # Agencies whose archived-feeds catalog couldn't be fetched (e.g. the
    # mdb_feed_id 404s). Cached so we skip OTP once per agency rather than
    # re-hitting the network for every one of that feed's days.
    failed_catalog: set[str] = set()

    def gtfs_for(feed: str, day: dt.date):
        info = feed_agency_map.get(feed)
        if info is None or not info[1]:
            return None
        agency_id, mdb_id = info
        if agency_id in failed_catalog:
            return None
        if agency_id not in cache:
            cache[agency_id] = GtfsResolver(
                mdb_id,
                agency_id.lower(),
                cache_dir=args.gtfs_cache_dir,
                api_url=args.gtfs_api_url,
            )
        try:
            return cache[agency_id].for_date(day)
        except requests.exceptions.RequestException as e:
            # Catalog/zip fetch failed for the whole agency — skip OTP for it.
            # (Per-date "no snapshot covers this day" raises LookupError, which
            # the caller handles separately; don't poison the agency for that.)
            print(
                f"[{feed}] GTFS catalog unavailable ({e}) — "
                f"skipping OTP for agency {agency_id}",
                file=sys.stderr,
            )
            failed_catalog.add(agency_id)
            return None

    return gtfs_for


def _normalized_path(curated_dir: Path, feed: str, day: dt.date) -> Path:
    return _partition_path(curated_dir / "normalized_vehicles", feed, day)


def _raw_table_path(curated_dir: Path, table: str, feed: str, day: dt.date) -> Path:
    return _partition_path(curated_dir / table, feed, day)


def load_feed_table_map(config_path: Path) -> dict[str, str]:
    """feed_name -> its single curated table, for feeds that have a Normalizer.

    The table is derived from the feed's decoder (`Decoder.produces`), so there's
    one source of truth. Feeds whose decoder emits multiple tables (the GTFS-RT
    `standard` decoder) or whose table has no registered normalizer are omitted.
    """
    from archiver.decoder import Decoder
    from analysis.normalize import Normalizer

    config = load_config(str(config_path))
    out: dict[str, str] = {}
    for agency in config.agencies:
        for feed in agency.feeds:
            specs = list(Decoder.from_name(feed.decoder).produces.values())
            if len(specs) != 1:
                continue
            table = specs[0].name
            if table in Normalizer._registry:
                out[feed.name] = table
    return out


_SWIV_LIGNE_TABLE = "swiv_ligne"


def load_swiv_topo_feed_map(config_path: Path) -> dict[str, str]:
    """agency_id -> the feed name whose decoder lands the swiv_ligne topo table.

    The Swiv route-name sidecar is a separate feed from the vehicles feed.
    Resolving its name from config (rather than assuming `{agency}-topo`) means
    topo resolution starts working the moment that feed is onboarded, under
    whatever name it's given. Empty until that feed exists.
    """
    from archiver.decoder import Decoder

    config = load_config(str(config_path))
    out: dict[str, str] = {}
    for agency in config.agencies:
        for feed in agency.feeds:
            specs = Decoder.from_name(feed.decoder).produces.values()
            if any(s.name == _SWIV_LIGNE_TABLE for s in specs):
                out[agency.agency_id] = feed.name
    return out


def _make_topo_resolver(curated_dir: Path, config_path: Path):
    """Build `topo_for(agency_id, day) -> {idLigne: nomCommercial} | None`.

    Reads the landed Swiv topo table (curated/swiv_ligne/feed=<topo feed>/...),
    with the topo feed name resolved from config. Best-effort: returns None when
    no topo feed is configured yet, or its partition isn't on disk — so Swiv
    routes degrade to unresolved, but with a one-time warning per agency rather
    than silently.
    """
    topo_feeds = load_swiv_topo_feed_map(config_path)
    cache: dict[tuple[str, dt.date], dict[str, str] | None] = {}
    warned: set[str] = set()

    def topo_for(agency_id: str, day: dt.date):
        key = (agency_id, day)
        if key in cache:
            return cache[key]
        topo_feed = topo_feeds.get(agency_id)
        result: dict[str, str] | None = None
        if topo_feed is not None:
            path = _raw_table_path(curated_dir, _SWIV_LIGNE_TABLE, topo_feed, day)
            if path.exists():
                tbl = pq.read_table(path).to_pylist()
                result = {
                    str(r["id_ligne"]): r["nom_commercial"]
                    for r in tbl
                    if r.get("id_ligne") is not None and r.get("nom_commercial")
                }
        if result is None and agency_id not in warned:
            warned.add(agency_id)
            print(
                f"[{agency_id}] Swiv topo table unavailable "
                f"(feed: {topo_feed or 'none configured'}) — idLigne routes "
                f"resolve by static map only",
                file=sys.stderr,
            )
        cache[key] = result
        return result

    return topo_for


def normalize_one(
    feed: str,
    day: dt.date,
    agency_id: str,
    tz: ZoneInfo,
    table: str,
    curated_dir: Path,
    *,
    gtfs_for,
    topo_for,
    force: bool,
) -> int | None:
    """Normalize one (feed, day) raw table into curated/normalized_vehicles.

    Best-effort, mirroring _build_otp: missing partition -> skip; no resolvable
    static GTFS -> skip. Returns the row count written, or None when skipped.
    """
    from analysis.normalize import NORMALIZED_VEHICLES_SCHEMA, Normalizer

    out = _normalized_path(curated_dir, feed, day)
    if out.exists() and not force:
        print(
            f"[{feed} {day}] normalized exists — skipping (use --force)",
            file=sys.stderr,
        )
        return None

    src = _raw_table_path(curated_dir, table, feed, day)
    if not src.exists():
        print(f"[{feed} {day}] SKIP normalize — no {table} partition", file=sys.stderr)
        return None

    try:
        gtfs = gtfs_for(feed, day)
    except (LookupError, FileNotFoundError) as e:
        print(f"[{feed} {day}] SKIP normalize — GTFS lookup: {e}", file=sys.stderr)
        return None
    if gtfs is None:
        print(
            f"[{feed} {day}] no static GTFS source — skipping normalize",
            file=sys.stderr,
        )
        return None

    normalizer = Normalizer.from_table(table)
    topo = topo_for(agency_id, day) if normalizer.needs_topo else None

    rows = pq.read_table(src).to_pylist()
    normalized = normalizer.normalize(
        rows, gtfs, feed=feed, agency_id=agency_id, agency_tz=tz, topo_map=topo
    )
    _write_parquet(normalized, NORMALIZED_VEHICLES_SCHEMA, out)

    # Count rows that landed at least one canonical GTFS id. Covers route feeds
    # (route_id) and LIRR (trip_id/stop_id), which carries no route token and so
    # would always read 0 against resolution_status alone.
    resolved = sum(
        1
        for r in normalized
        if r["route_id"] is not None
        or r["trip_id"] is not None
        or r["stop_id"] is not None
    )
    print(
        f"[{feed} {day}] {len(normalized):>7,} normalized  ({resolved:,} id-resolved)"
    )
    return len(normalized)


def _run_normalize(args: argparse.Namespace) -> int:
    """The --normalize path: build curated/normalized_vehicles for the JSON feeds."""
    feed_tz_map = load_feed_tz_map(args.config)
    feed_table_map = load_feed_table_map(args.config)
    feed_agency_map = load_feed_agency_map(args.config)
    gtfs_for = _make_gtfs_resolver(args)
    if gtfs_for is None:
        print(
            "[normalize] no GTFS resolver (pandas missing) — nothing to do",
            file=sys.stderr,
        )
        return 0
    topo_for = _make_topo_resolver(args.curated_dir, args.config)

    feeds = args.feed if args.feed else sorted(feed_table_map)
    base_dates = [args.day] if args.day else []
    total = 0
    for feed in feeds:
        table = feed_table_map.get(feed)
        if table is None:
            print(f"[{feed}] no normalizer — skipping", file=sys.stderr)
            continue
        agency_id = feed_agency_map[feed][0]
        tz = ZoneInfo(feed_tz_map[feed])
        dates = set(base_dates)
        if args.all_days:
            dates |= set(discover_dates_for_dir(feed, args.curated_dir / table))
        if not dates:
            print(f"[{feed}] no dates to process — skipping", file=sys.stderr)
            continue
        for day in sorted(dates):
            n = normalize_one(
                feed,
                day,
                agency_id,
                tz,
                table,
                args.curated_dir,
                gtfs_for=gtfs_for,
                topo_for=topo_for,
                force=args.force,
            )
            if n is not None:
                total += n
    print(f"---\ntotal: {total:,} normalized vehicle rows")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.normalize:
        return _run_normalize(args)
    feed_tz_map = load_feed_tz_map(args.config)
    feeds = args.feed if args.feed else sorted(feed_tz_map)
    base_dates = [args.day] if args.day else []
    gtfs_for = None if args.no_otp else _make_gtfs_resolver(args)

    totals = {
        "stop": 0,
        "route": 0,
        "events": 0,
        "adherence": 0,
        "on_time": 0,
        "segments": 0,
    }
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
        summary += f", {totals['segments']:,} segment-speed rows"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
