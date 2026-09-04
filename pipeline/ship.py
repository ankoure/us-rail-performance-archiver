import sys
from pathlib import Path

# Make the repo root importable when run as `python pipeline/ship.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from archiver.loader import build_shipper, load_config  # noqa: E402
from datetime import date
import argparse
import logging

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ship archived feeds from landing/curated to S3"
    )
    parser.add_argument("--feed", help="Restrict to one feed name")
    parser.add_argument(
        "--day", type=date.fromisoformat, help="Restrict to one day (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-upload even if keys already exist",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--hot-only",
        action="store_true",
        help="skip cold (tarball) upload; ship only curated parquets to hot bucket. "
        "Use after re-rolling parquets when the raw bins haven't changed, to avoid "
        "DEEP_ARCHIVE early-deletion fees.",
    )
    which.add_argument(
        "--cold-only",
        action="store_true",
        help="ship ONLY the cold tarball, which is built from the landing zone and "
        "needs nothing from the curated tree. This is the archive-first step in "
        "pipeline/agency_batch.py: it runs before rollup/gold so a failure there "
        "can't cost the raw archive, which landing's lifecycle rule expires "
        "whether or not it was ever shipped.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config = load_config(args.config)
    shipper = build_shipper(config)
    shipper.run(
        feed=args.feed,
        day=args.day,
        force=args.force,
        hot_only=args.hot_only,
        cold_only=args.cold_only,
    )


if __name__ == "__main__":
    args = parse_args()
    main(args)
