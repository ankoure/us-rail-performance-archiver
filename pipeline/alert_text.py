"""Build the versioned alert-translation tables from raw landing polls.

For each (feed, day), folds the day's polls into run tables
(analysis.alert_text) and writes them under:

    {curated}/alert_text/feed={feed}/year=/month=/day=/data.parquet
    {curated}/alert_versions/feed={feed}/year=/month=/day=/data.parquet
    {curated}/alert_payloads/feed={feed}/year=/month=/day=/data.parquet

The hot ship picks these up with the rest of curated (Shipper._curated_parquets
globs */feed=.../data.parquet), so there's no separate upload step.

Days are built in ascending order per feed, because run_ids and state carry
from the previous day's output. That output is read from local curated first,
then from the hot bucket (the nightly's container starts with an empty disk).

The nightly calls this from pipeline/snapshot.py for ALERT_TEXT_FEEDS only;
the standalone CLI is for backfills and one-offs:

    uv run python pipeline/alert_text.py --day 2026-09-27
    uv run python pipeline/alert_text.py --all-days --landing-dir /tmp/restored
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Make the repo root importable when run as `python pipeline/alert_text.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.alert_text import (  # noqa: E402
    build_alert_tables,
    load_prev_context,
    partition_path,
    schema_markdown,
    write_alert_tables,
)
from archiver.config import ArchiverConfig  # noqa: E402
from archiver.decoder import AlertRow  # noqa: E402
from archiver.loader import build_feeds, build_source, load_config  # noqa: E402
from archiver.source import LocalSource  # noqa: E402

load_dotenv()

# Feeds the nightly builds. MBTA only until lost-in-transit has validated the
# schema; widening is adding names here (see docs/design/alert-translations.md).
ALERT_TEXT_FEEDS = frozenset(
    {"massachusetts-bay-transportation-authority-mbta-alerts-1603"}
)

SCHEMA_DOC = Path(__file__).resolve().parent.parent / "docs/schemas/alert_text.md"


def mdb_feed_ids(config: ArchiverConfig) -> dict[str, str | None]:
    """{feed name: its feed-level mdb_feed_id} from config."""
    return {
        feed.name: feed.mdb_feed_id
        for agency in config.agencies
        for feed in agency.feeds
    }


def prev_roots(config: ArchiverConfig, curated_dir: Path) -> list[Path | str]:
    """Where to look for the previous day's output: local, then hot."""
    roots: list[Path | str] = [curated_dir]
    if config.s3.enabled and config.s3.hot_bucket:
        prefix = config.s3.hot_prefix.strip("/")
        roots.append(f"s3://{config.s3.hot_bucket}" + (f"/{prefix}" if prefix else ""))
    return roots


def build_alert_text_one(
    feed,
    day: date,
    source,
    curated_dir: Path,
    roots: list[Path | str],
    *,
    mdb_feed_id: str | None,
    force: bool,
) -> bool:
    """Build+write one (feed, day). Returns False if skipped (already built)."""
    marker = Path(partition_path(str(curated_dir), "alert_payloads", feed.name, day))
    if not force and marker.exists():
        print(f"[{feed.name} {day}] SKIP — alert_text already built", file=sys.stderr)
        return False

    prev = load_prev_context(roots, feed.name, day)
    tables = build_alert_tables(feed, day, source, prev, mdb_feed_id=mdb_feed_id)
    write_alert_tables(tables, curated_dir, feed.name, day)
    print(
        f"[{feed.name} {day}] alert_text: {tables['alert_text'].num_rows} text run(s), "
        f"{tables['alert_versions'].num_rows} version run(s), "
        f"{tables['alert_payloads'].num_rows} payload(s)"
        + ("" if prev else " (no previous day: runs start fresh)"),
        file=sys.stderr,
    )
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed",
        nargs="+",
        default=None,
        help="Feed names. Omit for ALERT_TEXT_FEEDS.",
    )
    p.add_argument(
        "--day", type=date.fromisoformat, help="A single day (YYYY-MM-DD) to build."
    )
    p.add_argument(
        "--all-days",
        action="store_true",
        help="Build every landing day available for each feed, oldest first.",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    p.add_argument(
        "--landing-dir",
        type=Path,
        default=None,
        help="Read landing from this local directory instead of the config's "
        "source, e.g. cold tarballs extracted for a backfill.",
    )
    p.add_argument(
        "--curated-dir",
        type=Path,
        default=None,
        help="Write here instead of the config's writer.curated_dir.",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Rebuild even if the day was already built.",
    )
    p.add_argument(
        "--write-schema-doc",
        action="store_true",
        help=f"Regenerate {SCHEMA_DOC} from the table schemas and exit.",
    )
    args = p.parse_args(argv)
    if not args.day and not args.all_days and not args.write_schema_doc:
        p.error("provide --day YYYY-MM-DD or --all-days")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.write_schema_doc:
        SCHEMA_DOC.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_DOC.write_text(schema_markdown())
        print(f"wrote {SCHEMA_DOC}")
        return 0
    config = load_config(args.config)
    source = LocalSource(args.landing_dir) if args.landing_dir else build_source(config)
    curated_dir = args.curated_dir or config.writer.curated_dir
    roots = prev_roots(config, curated_dir)
    mdb = mdb_feed_ids(config)
    wanted = set(args.feed) if args.feed else ALERT_TEXT_FEEDS
    feeds = [
        f
        for f in build_feeds(config)
        if f.name in wanted and AlertRow in f.decoder.produces
    ]

    today = datetime.now(timezone.utc).date()
    built = failed = 0
    for feed in feeds:
        dates = {args.day} if args.day else set()
        if args.all_days:
            dates |= {d for _, d in source.discover(feed.name) if d < today}
        for day in sorted(dates):
            try:
                if build_alert_text_one(
                    feed,
                    day,
                    source,
                    curated_dir,
                    roots,
                    mdb_feed_id=mdb.get(feed.name),
                    force=args.force,
                ):
                    built += 1
            except Exception as exc:
                # Later days of this feed would start fresh without this one,
                # so say so rather than carrying on quietly.
                failed += 1
                print(
                    f"[{feed.name} {day}] FAILED — {exc!r}; later days' runs "
                    "will not carry over from this day",
                    file=sys.stderr,
                )

    print(f"---\nbuilt {built} alert_text day(s), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
