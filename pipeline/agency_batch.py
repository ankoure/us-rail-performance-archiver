"""Run rollup -> [gtfs] -> gold -> ship per agency, each step a disposable
`python pipeline/X.py` subprocess, so the OS reclaims pandas/pyarrow allocations
agency-by-agency instead of one long-lived process accumulating them across all
~186 agencies (see terraform/rollup.tf's rollup_memory variable description for
the OOM history this replaces).

One agency's subprocess failure is logged and does not stop the others -- matches
the resilience philosophy already used for feed-level failures in gtfs.py/gold.py's
own loops. The process exit code is nonzero iff at least one agency failed, so the
ECS task still surfaces a bad day without losing the other agencies that succeeded.

Examples:

    uv run python pipeline/agency_batch.py --day 2026-08-17
    uv run python pipeline/agency_batch.py --day 2026-08-17 --agency BART METRO_STL -v
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Make the repo root importable when run as `python pipeline/agency_batch.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from archiver.logger import logger  # noqa: E402
from pipeline.agency_map import load_feed_agency_map  # noqa: E402

load_dotenv()


@dataclass
class AgencyResult:
    agency_id: str
    ok: bool
    failed_cmd: list[str] | None = None
    returncode: int = 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--day",
        type=date.fromisoformat,
        required=True,
        help="Day to process (YYYY-MM-DD)",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    p.add_argument(
        "--agency",
        nargs="+",
        default=None,
        help="Restrict to these agency_id(s) -- smoke testing / manual re-run",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("AGENCY_WORKERS", os.cpu_count() or 4)),
        help="Max agencies processed concurrently (env AGENCY_WORKERS)",
    )
    p.add_argument("-f", "--force", action="store_true")
    p.add_argument(
        "--include-gtfs",
        action="store_true",
        help="Also run pipeline/gtfs.py per agency, folded into this loop instead "
        "of a separate full-fleet step.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _agency_feed_groups(config_path: str) -> dict[str, list[str]]:
    """agency_id -> sorted feed names."""
    groups: dict[str, list[str]] = defaultdict(list)
    for feed_name, (agency_id, _mdb) in load_feed_agency_map(config_path).items():
        groups[agency_id].append(feed_name)
    return {aid: sorted(feeds) for aid, feeds in sorted(groups.items())}


def _rollup_cmd(feed: str, day: str, config: str, force: bool) -> list[str]:
    cmd = [
        sys.executable,
        "pipeline/rollup.py",
        "-c",
        config,
        "--feed",
        feed,
        "--day",
        day,
    ]
    return cmd + (["-f"] if force else [])


def _gtfs_cmd(feeds: list[str], day: str, config: str, force: bool) -> list[str]:
    # --feed uses nargs="+" and is greedy -- keep it last in the argv.
    cmd = [sys.executable, "pipeline/gtfs.py", "-c", config, "--day", day]
    cmd += (["-f"] if force else []) + ["--feed", *feeds]
    return cmd


def _gold_cmd(feeds: list[str], day: str, config: str, force: bool) -> list[str]:
    # --feed uses nargs="+" and is greedy -- keep it last in the argv.
    cmd = [sys.executable, "pipeline/gold.py", "-c", config, "--day", day]
    cmd += (["--force"] if force else []) + ["--feed", *feeds]
    return cmd


def _ship_cmd(feed: str, day: str, config: str, force: bool) -> list[str]:
    cmd = [
        sys.executable,
        "pipeline/ship.py",
        "-c",
        config,
        "--feed",
        feed,
        "--day",
        day,
    ]
    return cmd + (["--force"] if force else [])


def run_agency(
    agency_id: str,
    feeds: list[str],
    *,
    config: str,
    day: date,
    force: bool,
    include_gtfs: bool,
) -> AgencyResult:
    """rollup(each feed) -> [gtfs(all feeds)] -> gold(all feeds) -> ship(each feed).

    Stops at the first failing step and skips the rest of THIS agency's steps
    (e.g. a failed rollup means gold/ship never run against stale or partial
    curated data for it) but always returns rather than raising -- the caller
    moves on to the next agency regardless.
    """
    day_str = day.isoformat()
    steps: list[list[str]] = [_rollup_cmd(f, day_str, config, force) for f in feeds]
    if include_gtfs:
        steps.append(_gtfs_cmd(feeds, day_str, config, force))
    steps.append(_gold_cmd(feeds, day_str, config, force))
    steps += [_ship_cmd(f, day_str, config, force) for f in feeds]

    for cmd in steps:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            logger.error(
                "[%s] step failed (exit %d): %s",
                agency_id,
                result.returncode,
                " ".join(cmd),
            )
            return AgencyResult(agency_id, False, cmd, result.returncode)
    return AgencyResult(agency_id, True)


def run_all(
    groups: dict[str, list[str]],
    *,
    config: str,
    day: date,
    force: bool,
    include_gtfs: bool,
    workers: int,
) -> list[AgencyResult]:
    results: list[AgencyResult] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                run_agency,
                agency_id,
                feeds,
                config=config,
                day=day,
                force=force,
                include_gtfs=include_gtfs,
            ): agency_id
            for agency_id, feeds in groups.items()
        }
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    groups = _agency_feed_groups(args.config)
    if args.agency:
        missing = set(args.agency) - groups.keys()
        if missing:
            logger.warning("unknown --agency values, ignoring: %s", sorted(missing))
        groups = {a: f for a, f in groups.items() if a in args.agency}

    logger.info(
        "agency_batch: %d agencies, day=%s, workers=%d, include_gtfs=%s",
        len(groups),
        args.day,
        args.workers,
        args.include_gtfs,
    )

    results = run_all(
        groups,
        config=args.config,
        day=args.day,
        force=args.force,
        include_gtfs=args.include_gtfs,
        workers=args.workers,
    )

    failed = [r for r in results if not r.ok]
    logger.info(
        "agency_batch complete: %d/%d succeeded, %d failed",
        len(results) - len(failed),
        len(results),
        len(failed),
    )
    if failed:
        logger.error(
            "failed agencies: %s", ", ".join(sorted(r.agency_id for r in failed))
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
