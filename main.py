from concurrent.futures import ThreadPoolExecutor
import threading

from archiver.dispatcher import Dispatcher
from archiver.scheduler import Scheduler
from dotenv import load_dotenv
from archiver.loader import build_archiver, load_config
import argparse
import logging
import time
import signal

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
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=10,
        help="Determines how many threads to be spawned",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(threadName)s] %(name)s %(levelname)s: %(message)s",
        )

    config = load_config("config/feeds.yaml")
    archiver = build_archiver(config)
    scheduler = Scheduler(archiver.feeds, default_interval=args.frequency)

    # Liveness signal: a metric for alerting (no data => process down/hung) and
    # a local file the container HEALTHCHECK can stat for freshness. Refreshed
    # every loop tick, after the sleep, so it reflects an actively-running loop.
    heartbeat_path = config.writer.poll_state_dir / ".heartbeat"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)

    polls = 0
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            dispatcher = Dispatcher(
                scheduler, archiver, ex, telemetry=archiver.telemetry
            )
            while not stop.is_set() and (args.polls is None or polls < args.polls):
                due_at, feed = scheduler.next_due()
                now = time.monotonic()

                if due_at > now:
                    stop.wait(due_at - now)
                archiver.telemetry.gauge("poller.heartbeat", 1)
                heartbeat_path.touch()
                dispatcher.submit(feed)
                polls += 1
    finally:
        archiver.writer.flush_all()  # flush everything on shutdown


if __name__ == "__main__":
    args = parse_args()
    main(args)
