"""Export gobble-style ARR-only events.csv files from MARTA predictions.

MARTA's realtime is a prediction stream, not vehicle positions — so we can
only recover arrivals (when a (train, station) prediction's waiting_seconds
settles near zero and then drops out). No DEP events.

Examples:

    uv run python scripts/export_marta_events.py \\
        --feed marta-traindata --date 2026-05-12

    # date range
    uv run python scripts/export_marta_events.py \\
        --feed marta-traindata --start 2026-05-10 --end 2026-05-16
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import MartaDay, export_marta_events_csv  # noqa: E402

DEFAULT_TZ = "America/New_York"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed", default="marta-traindata", help="Feed name (default: marta-traindata)")
    p.add_argument(
        "--tz",
        default=DEFAULT_TZ,
        help=f"IANA timezone for local service date (default: {DEFAULT_TZ})",
    )
    p.add_argument("--date", nargs="+", type=dt.date.fromisoformat, help="One or more YYYY-MM-DD dates")
    p.add_argument("--start", type=dt.date.fromisoformat, help="Inclusive range start")
    p.add_argument("--end", type=dt.date.fromisoformat, help="Inclusive range end")
    p.add_argument("--curated-dir", type=Path, default=Path("curated"))
    p.add_argument(
        "--max-wait-for-arrival-s",
        type=int,
        default=120,
        help="Drop arrivals whose smallest observed waiting_seconds exceeds this (default: 120)",
    )
    p.add_argument(
        "--approach-split-gap-s",
        type=int,
        default=600,
        help="Split same (train, station, dest) into separate approaches when next_arr jumps "
        "by more than this many seconds (default: 600 = 10 min)",
    )
    args = p.parse_args(argv)
    if not args.date and not (args.start and args.end):
        p.error("provide --date YYYY-MM-DD [...] or --start YYYY-MM-DD --end YYYY-MM-DD")
    if (args.start and not args.end) or (args.end and not args.start):
        p.error("--start and --end must be given together")
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
    return sorted(set(dates))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tz = ZoneInfo(args.tz)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"Unknown timezone: {args.tz!r}")

    dates = resolve_dates(args)
    total_rows = 0
    total_files = 0
    for d in dates:
        try:
            day = MartaDay(
                args.feed,
                d,
                base_dir=args.curated_dir,
                max_wait_for_arrival_s=args.max_wait_for_arrival_s,
                approach_split_gap_s=args.approach_split_gap_s,
            )
            rows, files = export_marta_events_csv(day, tz, base_dir=args.curated_dir)
        except FileNotFoundError as e:
            print(f"[{d}] SKIP — {e}", file=sys.stderr)
            continue
        print(f"[{d}] {rows:>7,} rows  {files:>4} files")
        total_rows += rows
        total_files += files

    print(f"---\ntotal: {total_rows:,} rows across {total_files} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
