from dotenv import load_dotenv
from archiver.loader import build_archiver, load_config
import argparse
import logging
import time

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(description="Archive configured feeds")
    parser.add_argument(
        "-n",
        "--polls",
        type=int,
        default=None,
        help="Number of times to poll (omit for infinite)",
    )
    parser.add_argument(
        "-f",
        "--frequency",
        type=int,
        default=60,
        help="Seconds between polls (default: 60)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = load_config("config/feeds.yaml")
    archiver = build_archiver(config)

    polls = 0
    while args.polls is None or polls < args.polls:
        archiver.archive_once()
        polls += 1
        if args.polls is None or polls < args.polls:
            time.sleep(args.frequency)


if __name__ == "__main__":
    args = parse_args()
    main(args)
