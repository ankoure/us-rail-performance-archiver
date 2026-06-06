"""Build a TransitMatters-style daily alerts snapshot for one feed.

Reads the day's raw .bin files chronologically and produces a single JSON.gz
keyed by alert v3 ID with last-write-wins semantics.

Examples:

    # one day
    uv run python scripts/build_alert_snapshot.py --feed bart-alerts --date 2026-05-09

    # an inclusive range
    uv run python scripts/build_alert_snapshot.py --feed septa-alerts \\
        --start 2026-05-10 --end 2026-05-12
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from analysis.alert_snapshot import (  # noqa: E402
    build_alert_snapshot,
    write_alert_snapshot,
)
from archiver.loader import build_feeds, load_config  # noqa: E402

load_dotenv()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed", required=True, help="Feed name, e.g. bart-alerts")
    p.add_argument(
        "--date",
        nargs="+",
        type=dt.date.fromisoformat,
        help="One or more YYYY-MM-DD dates (UTC partition day)",
    )
    p.add_argument("--start", type=dt.date.fromisoformat, help="Inclusive range start")
    p.add_argument("--end", type=dt.date.fromisoformat, help="Inclusive range end")
    p.add_argument(
        "--config",
        type=Path,
        default=Path("config/feeds.yaml"),
        help="Path to feeds.yaml (default: ./config/feeds.yaml)",
    )
    p.add_argument(
        "--landing-dir",
        type=Path,
        default=Path("archive"),
        help="Where to read raw .bin files (default: ./archive)",
    )
    p.add_argument(
        "--analysis-dir",
        type=Path,
        default=Path("analysis"),
        help="Where to write snapshots/ under (default: ./analysis)",
    )
    args = p.parse_args(argv)
    if not args.date and not (args.start and args.end):
        p.error("provide --date YYYY-MM-DD [...] or --start --end")
    if (args.start and not args.end) or (args.end and not args.start):
        p.error("--start and --end must be given together")
    return args


def resolve_dates(args: argparse.Namespace) -> list[dt.date]:
    """Sorted, deduped dates from --date plus the inclusive --start..--end range."""
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
    """Build and write one alert snapshot per requested day; days with no alerts are skipped."""
    args = parse_args(argv)
    config = load_config(str(args.config))
    feeds = {f.name: f for f in build_feeds(config)}
    if args.feed not in feeds:
        raise SystemExit(f"Unknown feed: {args.feed!r}. Known: {sorted(feeds)}")
    feed = feeds[args.feed]

    for d in resolve_dates(args):
        snapshot = build_alert_snapshot(feed, d, landing_dir=args.landing_dir)
        n_alerts = len(snapshot["alerts"])
        if n_alerts == 0:
            print(f"[{d}] no alerts found, skipping write", file=sys.stderr)
            continue
        out = write_alert_snapshot(snapshot, base_dir=args.analysis_dir)
        total_polls = sum(a["poll_count"] for a in snapshot["alerts"].values())
        print(f"[{d}] {n_alerts:>4} alerts, {total_polls:>6,} appearances -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
