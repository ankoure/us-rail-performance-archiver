"""Backfill gold.py's metrics marts for past days, without re-running rollup.py.

gold.py's only real input is the curated `vehicles` silver parquet
(analysis/vehicle_day.py's VehicleDay.partition_path is a local Path); rollup.py
already produced it and ship.py already shipped it to the hot bucket, days or
weeks ago. Decoding it again from raw landing (which only survives 7 days --
terraform/landing.tf) is unnecessary AND wrongly caps how far back a reprocess
can reach. Instead: pull the already-shipped vehicles parquet back down into
the local partition layout gold.py expects, rebuild metrics locally, and ship
only the metrics marts back.

Ships via Shipper.ship_one(..., hot_only=True) directly rather than
pipeline/ship.py's CLI: Shipper.run()'s discovery always gates on the LANDING
partition existing (see Shipper._discover / archiver/source.py's
discover()), which is exactly the data this script assumes has already
expired. ship_one() with hot_only=True never touches landing at all.

Generalizes deploy/reprocess_day.sh (single feed, single day, run by hand) to
a feed list x day range; meant to run as the rail-archiver-gold-backfill ECS
task (terraform/gold_backfill.tf) via `aws ecs run-task` with FEEDS/START_DAY/
END_DAY overrides, but runs the same locally. A (feed, day) with no shipped
vehicles parquet is skipped, not fatal -- gaps are expected (feed onboarded
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

# Make the repo root importable when run as `python pipeline/gold_backfill.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from archiver.loader import (  # noqa: E402
    build_shipper,
    build_telemetry,
    build_uploader,
    load_config,
)
from archiver.logger import logger  # noqa: E402

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
        help="First day (YYYY-MM-DD) to reprocess.",
    )
    p.add_argument(
        "--end-day",
        type=dt.date.fromisoformat,
        help="Last day (YYYY-MM-DD), inclusive. Defaults to --start-day.",
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


def _vehicles_key(prefix: str, feed: str, day: dt.date) -> str:
    return (
        f"{prefix}vehicles/feed={feed}/year={day.year}/month={day.month}/"
        f"day={day.day}/data.parquet"
    )


def _local_vehicles_path(curated_dir: Path, feed: str, day: dt.date) -> Path:
    return (
        curated_dir
        / "vehicles"
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

    done = skipped = 0
    for feed in args.feed:
        for day in _daterange(args.start_day, end_day):
            key = _vehicles_key(config.s3.hot_prefix, feed, day)
            if not uploader.exists(hot_bucket, key):
                logger.info("[%s %s] no shipped vehicles parquet — skipping", feed, day)
                skipped += 1
                continue

            local = _local_vehicles_path(curated_dir, feed, day)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(uploader.get_bytes(hot_bucket, key))
            logger.info("[%s %s] pulled shipped vehicles parquet", feed, day)

            subprocess.run(
                [
                    sys.executable,
                    "pipeline/gold.py",
                    "--config",
                    args.config,
                    "--feed",
                    feed,
                    "--day",
                    day.isoformat(),
                    "--force",
                ],
                check=True,
            )
            # hot_only, bypassing Shipper.run()'s discovery -- see module docstring.
            shipper.ship_one(feed, day, force=True, hot_only=True)
            done += 1

    logger.info(
        "backfill complete: %d (feed, day) reprocessed, %d skipped (no shipped source)",
        done,
        skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
