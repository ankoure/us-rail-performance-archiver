"""Backfill gold.py's metrics marts for past days, without re-running rollup.py.

gold.py's only real input is a curated silver parquet -- `vehicles` for most
feeds, `trip_updates` for gold.py's `_TRIP_UPDATES_ONLY_FEEDS` (imported from
there so the two lists can't drift) -- and rollup.py already produced it, and
ship.py already shipped it to the hot bucket, days or weeks ago. Decoding it
again from raw landing (which only survives 7 days -- terraform/landing.tf)
is unnecessary AND wrongly caps how far back a reprocess can reach. Instead:
pull the already-shipped silver parquet back down into the local partition
layout gold.py expects, rebuild metrics locally, and ship only the metrics
marts back.

The day range is in local *service days* (gold's mart partition key; see
pipeline/gold.py's module docstring). One service day overlaps two UTC silver
partitions, so each one is pulled before gold runs with --service-date.
A partition that isn't in S3 is a warning, not a skip: the feed may really have
had no data that UTC day, but the service day is then built from partial data.

Ships via Shipper.ship_marts() directly rather than pipeline/ship.py's CLI:
Shipper.run()'s discovery always gates on the LANDING partition existing (see
Shipper._discover / archiver/source.py's discover()), which is exactly the
data this script assumes has already expired. ship_marts() uploads only the
rebuilt marts, never landing or the silver it just pulled down.

This is also how to repair marts built before gold partitioned by service day
(issue #118): rebuilding a service day overwrites its partition, so a range
backfill replaces the old split rows.

Generalizes deploy/reprocess_day.sh (single feed, single day, run by hand) to
a feed list x day range; meant to run as the rail-archiver-gold-backfill ECS
task (terraform/gold_backfill.tf) via `aws ecs run-task` with FEEDS/START_DAY/
END_DAY overrides, but runs the same locally. A (feed, day) with no shipped
silver parquet is skipped, not fatal -- gaps are expected (feed onboarded
partway through the range, etc.); a gold.py failure IS fatal, same as the
rest of the batch.

Example:
    uv run python pipeline/gold_backfill.py --feed wmata-vehicles mbta-vehicles \
        --start-day 2026-07-20 --end-day 2026-08-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# Make the repo root importable when run as `python pipeline/gold_backfill.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from archiver.loader import (
    build_shipper,
    build_telemetry,
    build_uploader,
    load_config,
)
from archiver.logger import logger
from analysis.timeutil import utc_days_for_service_day
from pipeline.gold import _SOURCE_SUBDIR, _TRIP_UPDATES_ONLY_FEEDS

load_dotenv()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed", nargs="+", required=True, help="One or more feed names to backfill."
    )
    p.add_argument(
        "--start-day",
        type=dt.date.fromisoformat,
        required=True,
        help="First local service day (YYYY-MM-DD) to reprocess.",
    )
    p.add_argument(
        "--end-day",
        type=dt.date.fromisoformat,
        help="Last local service day (YYYY-MM-DD), inclusive. Defaults to --start-day.",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def _table_for_feed(feed: str) -> str:
    """The silver table gold.py will read for `feed` -- mirrors its own auto-pick."""
    source = "trip-updates" if feed in _TRIP_UPDATES_ONLY_FEEDS else "vehicles"
    return _SOURCE_SUBDIR[source]


def _silver_key(prefix: str, table: str, feed: str, day: dt.date) -> str:
    return (
        f"{prefix}{table}/feed={feed}/year={day.year}/month={day.month}/"
        f"day={day.day}/data.parquet"
    )


def _local_silver_path(curated_dir: Path, table: str, feed: str, day: dt.date) -> Path:
    return (
        curated_dir
        / table
        / f"feed={feed}"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
        / "data.parquet"
    )


def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    end_day = args.end_day or args.start_day

    telemetry = build_telemetry(config.telemetry)
    uploader = build_uploader(config.s3, telemetry)
    shipper = build_shipper(config)
    curated_dir = config.writer.curated_dir
    hot_bucket = config.s3.hot_bucket

    feed_tz = {
        feed.name: ZoneInfo(agency.timezone)
        for agency in config.agencies
        for feed in agency.feeds
    }

    done = skipped = 0
    for feed in args.feed:
        table = _table_for_feed(feed)
        tz = feed_tz.get(feed)
        if tz is None:
            logger.error("[%s] not in %s — skipping", feed, args.config)
            skipped += 1
            continue
        for day in _daterange(args.start_day, end_day):
            utc_days = utc_days_for_service_day(day, tz)
            pulled = []
            for utc_day in utc_days:
                key = _silver_key(config.s3.hot_prefix, table, feed, utc_day)
                local = _local_silver_path(curated_dir, table, feed, utc_day)
                if local.exists():  # pulled for the previous service day
                    pulled.append(utc_day)
                    continue
                if not uploader.exists(hot_bucket, key):
                    continue
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(uploader.get_bytes(hot_bucket, key))
                pulled.append(utc_day)
            if not pulled:
                logger.info(
                    "[%s %s] no shipped %s parquet — skipping", feed, day, table
                )
                skipped += 1
                continue
            if len(pulled) < len(utc_days):
                logger.warning(
                    "[%s %s] building from partial silver: have UTC %s of %s",
                    feed,
                    day,
                    [d.isoformat() for d in pulled],
                    [d.isoformat() for d in utc_days],
                )
            logger.info("[%s %s] pulled shipped %s parquet", feed, day, table)

            subprocess.run(
                [
                    sys.executable,
                    "pipeline/gold.py",
                    "--config",
                    args.config,
                    "--service-date",
                    day.isoformat(),
                    "--force",
                    "--feed",
                    feed,
                ],
                check=True,
            )
            # Marts only, bypassing Shipper.run()'s discovery -- see module docstring.
            shipper.ship_marts(feed, day)
            done += 1

    logger.info(
        "backfill complete: %d (feed, day) reprocessed, %d skipped (no shipped source)",
        done,
        skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
