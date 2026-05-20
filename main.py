from archiver.logger import logger
from archiver.scheduler import Scheduler
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
    scheduler = Scheduler(archiver.feeds, default_interval=args.frequency)

    polls = 0
    while args.polls is None or polls < args.polls:
        due_at, feed = scheduler.next_due()
        now = time.monotonic()
        if due_at > now:
            time.sleep(due_at - now)
        poll_start = time.monotonic()
        archiver.archive_one(feed)
        poll_duration = time.monotonic() - poll_start
        interval = feed.poll_interval_seconds or args.frequency

        if poll_duration > interval:
            logger.warning(
                "Poll for %s took %.2fs, exceeds configured interval %ds",
                feed.name,
                poll_duration,
                interval,
            )

        scheduler.mark_polled(feed)
        polls += 1

if __name__ == "__main__":
    args = parse_args()
    main(args)
