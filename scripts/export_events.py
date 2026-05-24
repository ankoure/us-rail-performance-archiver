"""Export gobble-style events.csv files for one feed and one or more days.

Examples:

    # one day
    uv run python scripts/export_events.py \\
        --feed wmata-vehicles --tz America/New_York --date 2026-05-20

    # multiple specific days
    uv run python scripts/export_events.py \\
        --feed wmata-vehicles --tz America/New_York \\
        --date 2026-05-18 2026-05-19 2026-05-20

    # an inclusive range
    uv run python scripts/export_events.py \\
        --feed wmata-vehicles --tz America/New_York \\
        --start 2026-05-18 --end 2026-05-21
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allow `from analysis import ...` when run as `python scripts/export_events.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import GtfsResolver, StaticGtfs, VehicleDay, export_events_csv  # noqa: E402
from analysis.gtfs_fetcher import DEFAULT_API_URL, DEFAULT_CACHE_DIR  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed", required=True, help="Feed name, e.g. wmata-vehicles")
    p.add_argument(
        "--tz",
        required=True,
        help="IANA timezone for the feed's local service date, e.g. America/New_York",
    )
    p.add_argument(
        "--date",
        nargs="+",
        type=dt.date.fromisoformat,
        help="One or more YYYY-MM-DD dates",
    )
    p.add_argument(
        "--start", type=dt.date.fromisoformat, help="Inclusive range start (YYYY-MM-DD)"
    )
    p.add_argument(
        "--end", type=dt.date.fromisoformat, help="Inclusive range end (YYYY-MM-DD)"
    )
    p.add_argument(
        "--curated-dir",
        type=Path,
        default=Path("curated"),
        help="Where to read vehicles parquet from and write events under (default: ./curated)",
    )
    p.add_argument(
        "--merge-gap-seconds",
        type=int,
        default=60,
        help="Collapse same-stop visits separated by <= this many seconds (default: 60). "
        "Set to 0 to disable.",
    )
    p.add_argument(
        "--gtfs",
        type=Path,
        default=None,
        help="Path to a GTFS static zip. If provided, scheduled_headway and scheduled_tt "
        "are populated by joining against this schedule.",
    )
    p.add_argument(
        "--mdb-feed-id",
        default=None,
        help="MobilityDatabase feed ID (e.g. mdb-1847). When provided with --agency, "
        "the export auto-fetches the static GTFS snapshot effective for each date "
        "via the archived_feeds catalog. Mutually exclusive with --gtfs.",
    )
    p.add_argument(
        "--agency",
        default=None,
        help="Agency slug used for the cached zip path (e.g. wmata). Required with --mdb-feed-id.",
    )
    p.add_argument(
        "--gtfs-cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Where to cache fetched GTFS zips (default: {DEFAULT_CACHE_DIR})",
    )
    p.add_argument(
        "--gtfs-api-url",
        default=DEFAULT_API_URL,
        help="Archived-feeds catalog base URL",
    )
    args = p.parse_args(argv)

    if not args.date and not (args.start and args.end):
        p.error(
            "provide --date YYYY-MM-DD [...] or --start YYYY-MM-DD --end YYYY-MM-DD"
        )
    if (args.start and not args.end) or (args.end and not args.start):
        p.error("--start and --end must be given together")
    if args.gtfs and args.mdb_feed_id:
        p.error("--gtfs and --mdb-feed-id are mutually exclusive")
    if args.mdb_feed_id and not args.agency:
        p.error("--agency is required when using --mdb-feed-id")
    return args


def resolve_dates(args: argparse.Namespace) -> list[dt.date]:
    dates: list[dt.date] = []
    if args.date:
        dates.extend(args.date)
    if args.start and args.end:
        if args.end < args.start:
            raise SystemExit("--end must be >= --start")
        d = args.start
        while d <= args.end:
            dates.append(d)
            d += dt.timedelta(days=1)
    # Dedupe + sort
    return sorted(set(dates))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        tz = ZoneInfo(args.tz)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"Unknown timezone: {args.tz!r}")

    dates = resolve_dates(args)
    static_gtfs = StaticGtfs(args.gtfs) if args.gtfs else None
    resolver = (
        GtfsResolver(
            args.mdb_feed_id,
            args.agency,
            cache_dir=args.gtfs_cache_dir,
            api_url=args.gtfs_api_url,
        )
        if args.mdb_feed_id
        else None
    )
    total_rows = 0
    total_files = 0
    for d in dates:
        try:
            day = VehicleDay(
                args.feed,
                d,
                base_dir=args.curated_dir,
                merge_gap_seconds=args.merge_gap_seconds,
            )
            gtfs_for_day = (
                static_gtfs
                if static_gtfs
                else (resolver.for_date(d) if resolver else None)
            )
            rows, files = export_events_csv(
                day, tz, base_dir=args.curated_dir, gtfs=gtfs_for_day
            )
        except FileNotFoundError as e:
            print(f"[{d}] SKIP — {e}", file=sys.stderr)
            continue
        except LookupError as e:
            print(f"[{d}] SKIP GTFS lookup — {e}", file=sys.stderr)
            continue
        print(f"[{d}] {rows:>8,} rows  {files:>5} files")
        total_rows += rows
        total_files += files

    print(f"---\ntotal: {total_rows:,} rows across {total_files} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
