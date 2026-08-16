from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import date

from archiver.logger import logger
from archiver.loader import build_rollup, load_config
from archiver.rollup import Rollup

_WORKER_ROLLUP: Rollup | None = None

MAX_ATTEMPTS = 3


def _init_worker(config_path: str) -> None:
    global _WORKER_ROLLUP
    config = load_config(config_path)
    _WORKER_ROLLUP = build_rollup(config)


def _run_one(feed_name: str, day: date, force: bool) -> tuple[str, date]:
    _WORKER_ROLLUP.rollup_one(feed_name, day, force=force)
    return (feed_name, day)


def _run_batch(
    pairs: list[tuple[str, date]],
    config_path: str,
    force: bool,
    workers: int,
    total: int,
    completed_so_far: int,
) -> tuple[list[tuple[str, date]], int]:
    """
    Submit `pairs` to a fresh ProcessPoolExecutor and drain it.

    Returns (broken_pairs, completed_so_far) where broken_pairs is the list
    of (feed, day) tuples that were still pending when the pool broke, and
    completed_so_far is the running count of pairs that got a real result
    (success or genuine per-feed failure) — used for progress logging across
    retries.
    """
    broken_pairs: list[tuple[str, date]] = []

    with ProcessPoolExecutor(
        max_workers=max(1, workers),
        initializer=_init_worker,
        initargs=(config_path,),
    ) as ex:
        futures = {ex.submit(_run_one, fn, d, force): (fn, d) for fn, d in pairs}
        for fut in as_completed(futures):
            fn, d = futures[fut]
            try:
                fut.result()
                completed_so_far += 1
            except BrokenProcessPool:
                # Not this pair's fault — a sibling worker died (e.g. OOM
                # kill) and took the whole pool down with it. Every other
                # future on this executor will raise the same thing, so just
                # collect this pair for retry on a fresh pool rather than
                # logging it as a real failure.
                broken_pairs.append((fn, d))
            except Exception:
                # A genuine per-pair failure (bad data, bug in rollup_one,
                # etc). Nothing to retry here.
                logger.exception("rollup failed: %s/%s", fn, d)
                completed_so_far += 1

            if completed_so_far % 10 == 0 or completed_so_far == total:
                logger.info("rollup progress: %d/%d", completed_so_far, total)

    return broken_pairs, completed_so_far


def run_parallel(rollup: Rollup, config_path: str, feed, day, force, workers: int):
    pairs = list(rollup.discover(feed=feed, day=day))
    total = len(pairs)
    if total == 0:
        return
    if workers == 1 or total == 1:
        for fn, d in pairs:
            rollup.rollup_one(fn, d, force=force)
        return

    completed = 0
    pending = pairs
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not pending:
            break
        if attempt > 1:
            logger.warning(
                "retrying %d pair(s) after broken process pool (attempt %d/%d)",
                len(pending),
                attempt,
                MAX_ATTEMPTS,
            )
        pending, completed = _run_batch(
            pending, config_path, force, workers, total, completed
        )

    if pending:
        for fn, d in pending:
            logger.error(
                "rollup permanently failed after %d attempts (broken process pool): %s/%s",
                MAX_ATTEMPTS,
                fn,
                d,
            )
