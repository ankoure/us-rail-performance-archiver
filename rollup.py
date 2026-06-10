import os

from dotenv import load_dotenv
from archiver.loader import build_rollup, load_config
from datetime import date
import argparse
import logging
from archiver.parallel import run_parallel

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Roll up archived feeds from landing zone to curated parquet"
    )
    parser.add_argument("--feed", help="Restrict to one feed name")
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
    parser.add_argument("-f", "--force", action="store_true")
    return parser.parse_args()


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    rollup_workers = int(os.environ.get("ROLLUP_WORKERS", os.cpu_count() or 1))

    config = load_config(args.config)
    rollup = build_rollup(config)
    run_parallel(
        rollup=rollup,
        config_path=args.config,
        feed=args.feed,
        day=args.day,
        force=args.force,
        workers=rollup_workers,
    )


if __name__ == "__main__":
    args = parse_args()
    main(args)
