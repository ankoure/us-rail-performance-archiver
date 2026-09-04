import sys
from pathlib import Path

# Make the repo root importable when run as `python pipeline/prune_s3.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from archiver.loader import build_shipper, load_config  # noqa: E402
import argparse
import logging
from datetime import date

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Prune shipped raw/metadata day-partitions from the landing zone. "
        "Deletes only days whose cold tarball is confirmed in S3, keeping the most "
        "recent --keep-days in S3 as a buffer."
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=3,
        help="Retain this many most-recent days locally (default: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be deleted without deleting anything",
    )
    parser.add_argument(
        "--day", type=date.fromisoformat, help="Restrict to one day (YYYY-MM-DD)"
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
    shipper.prune_s3(keep_days=args.keep_days, dry_run=args.dry_run, day=args.day)


if __name__ == "__main__":
    main(parse_args())
