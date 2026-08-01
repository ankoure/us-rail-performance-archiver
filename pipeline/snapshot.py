"""Build daily last-write-wins alert snapshots from raw landing polls.

For each (feed, day) where the feed's decoder can produce AlertRow, merges the
day's raw .bin polls into one deduplicated snapshot (analysis.alert_snapshot)
and writes it under:

    {curated}/snapshots/alerts/feed={feed}/year=/month=/day=/data.json.gz

Mirrors rollup.py's/gold.py's CLI shell so the prod batch chain can call
`python pipeline/snapshot.py --day "$DAY"` alongside them.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Make the repo root importable when run as `python pipeline/snapshot.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.alert_snapshot import (  # noqa: E402
    build_alert_snapshot,
    snapshot_path,
    write_alert_snapshot,
)
from archiver.decoder import AlertRow  # noqa: E402
from archiver.loader import build_feeds, build_source, load_config  # noqa: E402

load_dotenv()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed",
        nargs="+",
        default=None,
        help="One or more feed names. Omit to process every alerts-capable feed "
        "in the config.",
    )
    p.add_argument(
        "--day", type=date.fromisoformat, help="A single day (YYYY-MM-DD) to build."
    )
    p.add_argument(
        "--all-days",
        action="store_true",
        help="Build every landing day available for each feed (unioned with --day).",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Rebuild even if the snapshot already exists.",
    )
    args = p.parse_args(argv)
    if not args.day and not args.all_days:
        p.error("provide --day YYYY-MM-DD or --all-days")
    return args


def build_snapshot_one(feed, day, source, curated_dir, *, force: bool) -> bool:
    """Build+write one (feed, day) snapshot. Returns False if skipped (already exists)."""
    out_path = snapshot_path(curated_dir, feed.name, day)
    if not force and out_path.exists():
        print(f"[{feed.name} {day}] SKIP — snapshot already exists", file=sys.stderr)
        return False

    snapshot = build_alert_snapshot(feed, day, source)
    write_alert_snapshot(snapshot, base_dir=curated_dir)
    print(
        f"[{feed.name} {day}] wrote {len(snapshot['alerts'])} alert(s)",
        file=sys.stderr,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    source = build_source(config)
    feeds = [f for f in build_feeds(config) if AlertRow in f.decoder.produces]
    if args.feed:
        feeds = [f for f in feeds if f.name in set(args.feed)]

    today = datetime.now(timezone.utc).date()
    built = 0
    for feed in feeds:
        dates = {args.day} if args.day else set()
        if args.all_days:
            dates |= {d for _, d in source.discover(feed.name) if d < today}
        if not dates:
            print(f"[{feed.name}] no dates to process — skipping", file=sys.stderr)
            continue
        for day in sorted(dates):
            try:
                if build_snapshot_one(
                    feed, day, source, config.writer.curated_dir, force=args.force
                ):
                    built += 1
            except Exception as exc:
                print(f"[{feed.name} {day}] SKIP — {exc}", file=sys.stderr)

    print(f"---\nbuilt {built} alert snapshot(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
