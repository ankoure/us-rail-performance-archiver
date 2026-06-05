import asyncio
import contextlib

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
        help="Max concurrent in-flight polls (semaphore cap)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def run(args):
    config = load_config("config/feeds.yaml")
    archiver = build_archiver(config)
    scheduler = Scheduler(archiver.feeds, default_interval=args.frequency)

    # Liveness signal: a metric for alerting (no data => process down/hung) and
    # a local file the container HEALTHCHECK can stat for freshness. Refreshed
    # every loop tick, after the sleep, so it reflects an actively-running loop.
    heartbeat_path = config.writer.poll_state_dir / ".heartbeat"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)

    polls = 0
    stop = asyncio.Event()

    # Signals must wake an awaiting loop on the loop thread: add_signal_handler
    # schedules stop.set() on the loop, unlike signal.signal() which fires on an
    # arbitrary thread and can't safely touch loop state.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    async with contextlib.AsyncExitStack() as stack:
        # Enter every distinct agency client so their pools open now and close on
        # exit. One client is shared across all feeds of an agency, hence the set.
        for client in {feed.client for feed in archiver.feeds}:
            await stack.enter_async_context(client)

        inflight: set[asyncio.Task] = set()
        sem = asyncio.Semaphore(args.workers)

        def _on_done(task: asyncio.Task) -> None:
            inflight.discard(task)
            sem.release()

        # flush_due gating: windows are wall-clock-aligned, so every feed's window
        # closes on the same boundary -> one synchronized write burst. Flush only
        # when the window index advances (not every tick), and offload the burst to
        # a thread so it never blocks the loop / heartbeat. Safe because
        # BatchingWriter guards its buffer with a threading.Lock.
        window_seconds = config.writer.window_seconds
        last_window = None

        try:
            while not stop.is_set() and (args.polls is None or polls < args.polls):
                due_at, feed = scheduler.next_due()
                now = time.monotonic()

                delay = due_at - now
                if delay > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                if stop.is_set():
                    break

                archiver.telemetry.gauge("poller.heartbeat", 1)
                heartbeat_path.touch()

                if sem.locked():  # no free slot → shed this cycle
                    archiver.telemetry.incr("poll.skipped", tags={"feed": feed.name})
                else:
                    await (
                        sem.acquire()
                    )  # can't block: we just checked locked() is False
                    task = asyncio.create_task(archiver.archive_one(feed))
                    inflight.add(task)
                    task.add_done_callback(_on_done)
                scheduler.mark_polled(feed)  # reschedule either way

                # Wall-clock (NOT the monotonic `now` above): window keys are unix
                # seconds. Flush only when crossing into a new window.
                wall = time.time()
                current_window = int(wall // window_seconds)
                if current_window != last_window:
                    last_window = current_window
                    await asyncio.to_thread(archiver.writer.flush_due, wall)

                polls += 1
        finally:
            # Drain ordering (correctness): finish in-flight polls BEFORE flushing
            # (a poll may still be buffering bytes), and both BEFORE the stack
            # closes the clients (an in-flight poll is still using its client).
            await asyncio.gather(*inflight)
            archiver.writer.flush_all()


def main(args):
    # Sync boundary: set up logging, then cross into the event loop. This is the
    # one place asyncio.run is called.
    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(threadName)s] %(name)s %(levelname)s: %(message)s",
        )
    asyncio.run(run(args))


if __name__ == "__main__":
    args = parse_args()
    main(args)
