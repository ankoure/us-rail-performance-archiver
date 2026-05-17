from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date

from archiver.logger import logger
from archiver.loader import build_rollup, load_config
from archiver.rollup import Rollup


_WORKER_ROLLUP: Rollup | None = None


def _init_worker(config_path: str) -> None:
    global _WORKER_ROLLUP
    config = load_config(config_path)
    _WORKER_ROLLUP = build_rollup(config)


def _run_one(feed_name: str, day: date, force: bool) -> tuple[str, date]:
    _WORKER_ROLLUP.rollup_one(feed_name, day, force=force)
    return (feed_name, day)


def run_parallel(rollup: Rollup, config_path: str, feed, day, force, workers: int):
    pairs = list(rollup.discover(feed=feed, day=day))
    total = len(pairs)
    if total == 0:
        return
    if workers == 1 or total == 1:
        for fn, d in pairs:
            rollup.rollup_one(fn, d, force=force)
        return
    with ProcessPoolExecutor(
        max_workers=max(1, workers),
        initializer=_init_worker,
        initargs=(config_path,),
    ) as ex:
        futures = {ex.submit(_run_one, fn, d, force): (fn, d) for fn, d in pairs}
        for i, fut in enumerate(as_completed(futures), 1):
            fn, d = futures[fut]
            try:
                fut.result()
            except Exception:
                logger.exception("rollup failed: %s/%s", fn, d)
            if i % 10 == 0 or i == total:
                logger.info("rollup progress: %d/%d", i, total)
