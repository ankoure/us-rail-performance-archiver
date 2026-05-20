from dotenv import load_dotenv
from archiver.loader import build_shipper, load_config
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
    parser.add_argument(
        "--hot-only",
        action="store_true",
        help="skip cold (tarball) upload; ship only curated parquets to hot bucket. "
        "Use after re-rolling parquets when the raw bins haven't changed, to avoid "
        "DEEP_ARCHIVE early-deletion fees.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config = load_config("config/feeds.yaml")
    shipper = build_shipper(config)
    shipper.run(feed=args.feed, day=args.day, force=args.force, hot_only=args.hot_only)


if __name__ == "__main__":
    args = parse_args()
    main(args)
