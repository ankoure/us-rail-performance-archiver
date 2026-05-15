from dotenv import load_dotenv
from archiver.loader import build_rollup, load_config
from datetime import date
import argparse
import logging

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Roll up archived feeds from landing zone to curated parquet"
    )
    parser.add_argument("--feed", help="Restrict to one feed name")
    parser.add_argument(
        "--day", type=date.fromisoformat, help="Restrict to one day (YYYY-MM-DD)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-f", "--force", action="store_true")
    return parser.parse_args()


def main(args):
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = load_config("config/feeds.yaml")
    rollup = build_rollup(config)
    rollup.run(feed=args.feed, day=args.day, force=args.force)


if __name__ == "__main__":
    args = parse_args()
    main(args)
