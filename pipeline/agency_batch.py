"""Run ship --cold-only -> [rollup(feed) -> ship(feed)] per feed -> [gtfs] ->
gold -> [snapshot] -> ship per agency (see run_agency for why the cold ship
leads and each feed's rollup ships immediately), each step a disposable
`python pipeline/X.py` subprocess, so the OS reclaims pandas/pyarrow allocations
agency-by-agency instead of one long-lived process accumulating them across all
~186 agencies (see terraform/rollup.tf's rollup_memory variable description for
the OOM history this replaces).

One agency's subprocess failure is logged and does not stop the others -- matches
the resilience philosophy already used for feed-level failures in gtfs.py/gold.py's
own loops. The process exit code is nonzero iff at least one agency failed, so the
ECS task still surfaces a bad day without losing the other agencies that succeeded.

`--stages` runs only part of that chain, so each stage can live in its own ECS
task sized for its own bottleneck (rollup is CPU-bound, gtfs/gold/snapshot are
memory-bound, the ships are I/O-bound) instead of every stage being sized at the
max of all of them. Stages always execute in canonical STAGES order regardless of
the order they're passed in.

Examples:

    uv run python pipeline/agency_batch.py --day 2026-08-17
    uv run python pipeline/agency_batch.py --day 2026-08-17 --agency BART METRO_STL -v
    uv run python pipeline/agency_batch.py --day 2026-08-17 --exclude-agency GO_AHEAD
    uv run python pipeline/agency_batch.py --day 2026-08-17 --stages snapshot
    uv run python pipeline/agency_batch.py --day 2026-08-17 --stages cold-ship
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
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


# Canonical execution order. `cold-ship` leads (see run_agency); `hot-ship` is
# last because it uploads what every other stage produced.
STAGES: tuple[str, ...] = (
    "cold-ship",
    "rollup",
    "gtfs",
    "gold",
    "snapshot",
    "hot-ship",
)

# Stages that write to curated_dir and therefore need a hot ship afterwards, or
# their output never leaves the container's ephemeral disk.
_PRODUCER_STAGES = frozenset({"rollup", "gtfs", "gold", "snapshot"})


@dataclass
class AgencyResult:
    agency_id: str
    ok: bool
    failed_cmd: list[str] | None = None
    returncode: int = 0


def resolve_stages(
    stages: list[str] | None, *, include_gtfs: bool, include_snapshot: bool
) -> list[str]:
    """Canonically-ordered stage list for one run.

    `stages=None` reproduces the pre---stages behaviour exactly, driven by the
    older --include-gtfs/--include-snapshot booleans, so existing callers (and
    the combined nightly task) are unaffected.

    Otherwise the selection is honoured, with one correction: selecting any
    producer stage without `hot-ship` would write parquet to ephemeral disk that
    nothing ever uploads -- a silent no-op, which is the exact failure mode that
    sank the earlier stage-split attempt (see terraform/rollup.tf's NOTE). So
    hot-ship is appended rather than left to the caller to remember.
    """
    if stages is None:
        selected = {"cold-ship", "rollup", "gold", "hot-ship"}
        if include_gtfs:
            selected.add("gtfs")
        if include_snapshot:
            selected.add("snapshot")
    else:
        selected = set(stages)
        if selected & _PRODUCER_STAGES:
            selected.add("hot-ship")
    return [s for s in STAGES if s in selected]


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
        "--exclude-agency",
        nargs="+",
        default=None,
        help="Exclude these agency_id(s) -- for the main task to skip agencies "
        "split into their own, more-memory task (see terraform/rollup.tf's "
        "rollup_heavy task and local.heavy_agencies).",
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
    p.add_argument(
        "--include-snapshot",
        action="store_true",
        help="Also run pipeline/snapshot.py per agency, folded into this loop "
        "instead of a separate full-fleet step. snapshot.py has no identified "
        "unbounded cache, but 2026-08-18 proved that doesn't matter -- any step "
        "handling the whole fleet in one long-lived process is at risk of the "
        "same allocator-level OOM, cache bug or not.",
    )
    p.add_argument(
        "--stages",
        nargs="+",
        choices=STAGES,
        default=None,
        help="Run only these stages, in canonical order regardless of how they're "
        f"listed here. Choices: {', '.join(STAGES)}. Omit to run the full chain "
        "driven by --include-gtfs/--include-snapshot (the combined-task default). "
        "Selecting any stage that writes curated output implies hot-ship, so its "
        "parquet can't be stranded on ephemeral disk.",
    )
    p.add_argument(
        "--curated-dir",
        type=Path,
        default=Path("data/curated"),
        help="Curated root to CLEAN UP after each agency ships, so local disk "
        "doesn't accumulate the whole day's output before the task exits (see "
        "clean_agency_curated). NOTE: this does not redirect where the steps "
        "write -- rollup/snapshot/ship read curated_dir from the config's "
        "writer.curated_dir, and gold/gtfs default to data/curated. So this must "
        "MATCH the config value or cleanup silently targets the wrong tree. It "
        "is not a way to relocate a run's output.",
    )
    p.add_argument(
        "--silver-dir",
        default=None,
        help="Passed through to gold.py --silver-dir: where gold reads rollup's "
        "silver parquet from. Accepts an s3:// URI (the hot bucket IS the "
        "curated tree), which is what lets `--stages gold` run as its own ECS "
        "task instead of sharing ephemeral disk with rollup. Marts are still "
        "written to --curated-dir and shipped from there.",
    )
    p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip deleting local curated files after each agency ships -- for "
        "debugging a specific agency's output on disk.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def clean_agency_curated(curated_dir: Path, feeds: list[str]) -> None:
    """Delete every feed's local curated output (silver + gold + version-partitioned
    GTFS marts + snapshots) after it's been shipped to S3.

    Local disk is a single ~40 GiB volume shared by every agency this task
    processes, and nothing else in the pipeline ever reclaims it -- ship.py
    uploads to S3 but leaves the local copy in place. Left unchecked, a full
    day's cumulative curated output across ~186 agencies exceeds the disk before
    the run finishes (observed 2026-08-18: a run failed 25 agencies with "No
    space left on device", clustered in the back half of the agency list --
    i.e. disk filled up progressively, not from any one agency). S3 is already
    the durable copy once ship.py succeeds, so the local copy has no further use
    within this run.
    """
    for feed in feeds:
        for d in curated_dir.glob(f"**/feed={feed}"):
            shutil.rmtree(d, ignore_errors=True)


def _agency_feed_groups(config_path: str) -> dict[str, list[str]]:
    """agency_id -> sorted feed names."""
    groups: dict[str, list[str]] = defaultdict(list)
    for feed_name, (agency_id, _mdb) in load_feed_agency_map(config_path).items():
        groups[agency_id].append(feed_name)
    return {aid: sorted(feeds) for aid, feeds in sorted(groups.items())}


def _snapshot_cmd(feeds: list[str], day: str, config: str, force: bool) -> list[str]:
    # --feed uses nargs="+" and is greedy -- keep it last in the argv.
    cmd = [sys.executable, "pipeline/snapshot.py", "-c", config, "--day", day]
    cmd += (["-f"] if force else []) + ["--feed", *feeds]
    return cmd


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


def _gold_cmd(
    feeds: list[str],
    day: str,
    config: str,
    force: bool,
    *,
    silver_dir: str | None = None,
) -> list[str]:
    # --feed uses nargs="+" and is greedy -- keep it last in the argv.
    cmd = [sys.executable, "pipeline/gold.py", "-c", config, "--day", day]
    # Only gold reads another stage's output. Pointing it at the hot bucket is
    # what lets the gold stage run as its own task instead of sharing ephemeral
    # disk with rollup -- see analysis/curated_fs.py.
    cmd += ["--silver-dir", silver_dir] if silver_dir else []
    cmd += (["--force"] if force else []) + ["--feed", *feeds]
    return cmd


def _ship_cmd(
    feed: str, day: str, config: str, force: bool, *, cold_only: bool = False
) -> list[str]:
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
    cmd += ["--cold-only"] if cold_only else []
    return cmd + (["--force"] if force else [])


def run_agency(
    agency_id: str,
    feeds: list[str],
    *,
    config: str,
    day: date,
    force: bool,
    stages: list[str],
    curated_dir: Path,
    cleanup: bool,
    silver_dir: str | None = None,
) -> AgencyResult:
    """Run this agency's slice of `stages` (already canonically ordered by
    resolve_stages): ship --cold-only(each feed) -> [rollup(feed) -> ship(feed)]
    per feed -> gtfs(all feeds) -> gold(all feeds) -> snapshot(all feeds) ->
    ship(each feed).

    The leading cold ship is deliberate. The cold DEEP_ARCHIVE tarball is built
    from the landing zone alone (Shipper._ship_cold -> _build_tarball ->
    source.iter_bins) and depends on nothing the later steps produce, but it is
    the only output that can't be rebuilt: terraform/landing.tf expires landing
    objects after 7 days whether or not they were ever shipped. Running it last
    -- as this did until 2026-09-03 -- meant any failure in an earlier step
    silently cost the raw archive. It had: BKK/EDMONTON_TRANSIT_SYSTEM/
    METRO_HOUSTON lost days to repeated snapshot.py and gold.py SIGKILLs, and a
    missing TFNSW_API_KEY crashed snapshot.py during config load for the whole
    fleet on Aug 23-24, costing both days' tarballs for every agency. Archiving
    first makes a failure downstream cost a mart instead of the data.

    Each feed's hot ship runs immediately after ITS rollup, not deferred to a
    single end-of-chain ship -- same reasoning, one stage later. Before
    2026-09-05, GO_AHEAD's rollup parquet for 2026-08-26..31 was computed
    successfully but never reached S3 at all: gtfs.py OOMed right after, the
    fail-fast chain stopped there, and the old single hot-ship at the end of
    the chain never ran. resolve_stages guarantees "hot-ship" is in `stages`
    whenever "rollup" is, so this always fires when rollup does. Unlike
    rollup's own failure (which must still fail-fast -- gtfs/gold must not run
    against stale or partial curated data), a failure shipping what rollup just
    produced is non-fatal, exactly like the cold ship above: a transient S3
    hiccup here shouldn't also block gtfs/gold, and the catch-all hot-ship pass
    at the end of the chain retries it for free via ship.py's exists() check.

    For the same reason snapshot runs at the END of the fail-fast chain rather
    than the start: nothing reads its output except Shipper._ship_snapshots, so
    there's no reason for a snapshot OOM to also cost the rollup parquet and
    gold marts.

    The fail-fast chain stops at the first failing step and skips the rest of
    THIS agency's steps (e.g. a failed rollup means gold/ship never run against
    stale or partial curated data for it) but always returns rather than raising
    -- the caller moves on to the next agency regardless.

    Local curated output is cleaned up (unless `cleanup=False`) whether this
    agency succeeded or failed -- disk is shared across every agency still to
    come in this task, and a failed agency's partial output has no further use
    once landing (the real source of truth, in S3) can re-roll it on retry.
    """
    day_str = day.isoformat()

    # Non-fatal on purpose: a transient S3 failure here shouldn't skip the whole
    # agency, and the full ship at the end of the chain retries it -- when this
    # already succeeded, _ship_cold's exists() check makes that retry a single
    # HeadObject.
    if "cold-ship" in stages:
        for feed in feeds:
            cmd = _ship_cmd(feed, day_str, config, force, cold_only=True)
            if subprocess.run(cmd).returncode != 0:
                logger.error(
                    "[%s] archive-first cold ship failed for %s, continuing",
                    agency_id,
                    feed,
                )

    result_out = AgencyResult(agency_id, True)

    # rollup is handled outside the generic dispatch below (same reasoning as
    # cold-ship above) so each feed's hot-ship can fire right after ITS
    # rollup, before gtfs/gold get a chance to fail-fast the rest of the
    # chain -- see the docstring. rollup's own failure still fail-fasts
    # (result_out.ok gates the rest of the chain below); the interim
    # hot-ship's failure doesn't.
    if "rollup" in stages:
        for feed in feeds:
            cmd = _rollup_cmd(feed, day_str, config, force)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                logger.error(
                    "[%s] step failed (exit %d): %s",
                    agency_id,
                    result.returncode,
                    " ".join(cmd),
                )
                result_out = AgencyResult(agency_id, False, cmd, result.returncode)
                break
            if "hot-ship" in stages:
                ship_cmd = _ship_cmd(feed, day_str, config, force)
                if subprocess.run(ship_cmd).returncode != 0:
                    logger.error(
                        "[%s] interim silver ship failed for %s, continuing",
                        agency_id,
                        feed,
                    )

    # Per-feed stages get one invocation each; whole-agency stages get one
    # invocation with every feed name (their --feed is nargs="+" and greedy).
    # rollup is deliberately absent here -- handled above.
    per_agency = {"gtfs": _gtfs_cmd, "gold": _gold_cmd, "snapshot": _snapshot_cmd}
    steps: list[list[str]] = []
    if result_out.ok:
        for stage in stages:
            if stage == "hot-ship":
                steps += [_ship_cmd(f, day_str, config, force) for f in feeds]
            elif stage == "gold":
                steps.append(
                    _gold_cmd(feeds, day_str, config, force, silver_dir=silver_dir)
                )
            elif stage in per_agency:
                steps.append(per_agency[stage](feeds, day_str, config, force))

    for cmd in steps:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            logger.error(
                "[%s] step failed (exit %d): %s",
                agency_id,
                result.returncode,
                " ".join(cmd),
            )
            result_out = AgencyResult(agency_id, False, cmd, result.returncode)
            break

    if cleanup:
        clean_agency_curated(curated_dir, feeds)
    return result_out


def run_all(
    groups: dict[str, list[str]],
    *,
    config: str,
    day: date,
    force: bool,
    stages: list[str],
    workers: int,
    curated_dir: Path,
    cleanup: bool,
    silver_dir: str | None = None,
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
                stages=stages,
                curated_dir=curated_dir,
                cleanup=cleanup,
                silver_dir=silver_dir,
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
    if args.exclude_agency:
        groups = {a: f for a, f in groups.items() if a not in args.exclude_agency}

    stages = resolve_stages(
        args.stages,
        include_gtfs=args.include_gtfs,
        include_snapshot=args.include_snapshot,
    )
    logger.info(
        "agency_batch: %d agencies, day=%s, workers=%d, stages=%s",
        len(groups),
        args.day,
        args.workers,
        " ".join(stages),
    )

    results = run_all(
        groups,
        config=args.config,
        day=args.day,
        force=args.force,
        stages=stages,
        workers=args.workers,
        curated_dir=args.curated_dir,
        cleanup=not args.no_cleanup,
        silver_dir=args.silver_dir,
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
