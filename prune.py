from dotenv import load_dotenv
from archiver.loader import build_shipper, load_config
import argparse
import logging

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Prune shipped raw/metadata day-partitions from the landing zone. "
        "Deletes only days whose cold tarball is confirmed in S3, keeping the most "
        "recent --keep-days locally as a buffer."
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
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config("config/feeds.yaml")
    shipper = build_shipper(config)
    shipper.prune(keep_days=args.keep_days, dry_run=args.dry_run)


if __name__ == "__main__":
    main(parse_args())
