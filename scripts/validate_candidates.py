"""Poll each candidate feed once and bucket it OK / format-mismatch / needs-auth / dead.

The generator (scripts/gen_feeds_from_mdb.py) emits a structurally-valid candidate
YAML, but nothing has checked that the URLs actually serve standard GTFS-rt. This
script does one real GET per feed, classifies the outcome, prints a report, and
writes a filtered YAML containing only the OK feeds — the subset safe to merge into
config/feeds.yaml.

It reuses the live machinery (build_feeds/build_client + parse_response) so feeds
are fetched and parsed exactly as the poller would.

Buckets:
  ok              200 + parses as GTFS-rt with the standard decoder
  format_mismatch 200 but not parseable as standard GTFS-rt (wrong format / drift)
  needs_auth      401/403 — the catalog said no-auth but the endpoint disagrees
  dead            timeout / DNS / connection error / 404 / 5xx

Usage:
    uv run python scripts/validate_candidates.py \\
        --candidates config/feeds.candidates.yaml \\
        --out config/feeds.candidates.validated.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.config import ArchiverConfig  # noqa: E402
from archiver.feed import Feed  # noqa: E402
from archiver.loader import build_feeds  # noqa: E402
from archiver.logger import logger  # noqa: E402
from archiver.parser import parse_response  # noqa: E402
from archiver.response import (  # noqa: E402
    DecodeFailureResponse,
    ErrorResponse,
    ProtobufResponse,
    UnknownResponse,
)

# Bucket labels (also the order they print in).
OK = "ok"
FORMAT_MISMATCH = "format_mismatch"
NEEDS_AUTH = "needs_auth"
DEAD = "dead"


def load_candidates(path: Path) -> tuple[dict, list[Feed]]:
    """Return (raw YAML dict, built Feeds).

    The candidate file holds only ``agencies:``; ArchiverConfig needs a writer
    block, so wrap it with a throwaway one (never written to — we only build
    feeds, not a writer). All candidates are no-auth, so build_client reads no
    env vars.
    """
    raw = yaml.safe_load(path.read_text())
    config = ArchiverConfig.model_validate(
        {
            "writer": {
                "writer_type": "local",
                "landing_dir": "/tmp/_validate_unused",
                "curated_dir": "/tmp/_validate_unused",
            },
            "agencies": raw.get("agencies", []),
        }
    )
    return raw, build_feeds(config)


async def validate_one(
    feed: Feed, sem: asyncio.Semaphore, timeout: int
) -> tuple[str, str, str]:
    """One GET; return (feed_name, bucket, detail)."""
    async with sem:
        try:
            http = await feed.client.get(feed.path, timeout=timeout)
        except Exception as exc:  # httpx timeout / connect / DNS / etc.
            return feed.name, DEAD, f"{type(exc).__name__}: {exc}"

    resp = parse_response(http, feed.parser, feed.decoder)

    if isinstance(resp, ErrorResponse):
        if resp.status_code in (401, 403):
            return feed.name, NEEDS_AUTH, f"HTTP {resp.status_code}"
        return feed.name, DEAD, f"HTTP {resp.status_code}"
    if isinstance(resp, UnknownResponse):
        return feed.name, FORMAT_MISMATCH, "did not parse as GTFS-rt protobuf"
    if isinstance(resp, DecodeFailureResponse):
        return feed.name, FORMAT_MISMATCH, "parsed but missing required GTFS-rt fields"

    # ProtobufResponse / JsonResponse -> OK. Note empties (valid but data-less now).
    detail = ""
    if isinstance(resp, ProtobufResponse):
        n = len(resp.parsed_message().entity)
        detail = f"{n} entities" + (" (EMPTY)" if n == 0 else "")
    return feed.name, OK, detail


async def validate_all(
    feeds: list[Feed], concurrency: int, timeout: int
) -> list[tuple[str, str, str]]:
    sem = asyncio.Semaphore(concurrency)
    async with contextlib.AsyncExitStack() as stack:
        # One client per agency (shared across its feeds) — open the pools now,
        # close them on exit, same as the poll loop does.
        for client in {feed.client for feed in feeds}:
            await stack.enter_async_context(client)
        return await asyncio.gather(*(validate_one(f, sem, timeout) for f in feeds))


def write_validated(raw: dict, ok_names: set[str], out_path: Path) -> int:
    """Write a candidate YAML keeping only OK feeds; drop now-empty agencies.

    Returns the number of agencies written.
    """
    header = (
        "# VALIDATED CANDIDATES — only feeds that returned standard GTFS-rt.\n"
        "# Produced by scripts/validate_candidates.py from the generator output.\n"
        "# Still review before merging into config/feeds.yaml.\n\n"
    )
    out_agencies = []
    for agency in raw.get("agencies", []):
        kept = [f for f in agency.get("feeds", []) if f.get("name") in ok_names]
        if kept:
            out_agencies.append({**agency, "feeds": kept})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        fh.write(header)
        yaml.safe_dump(
            {"agencies": out_agencies}, fh, allow_unicode=True, sort_keys=False
        )
    return len(out_agencies)


def report(results: list[tuple[str, str, str]]) -> None:
    """Print a per-bucket summary to stderr."""
    by_bucket: dict[str, list[tuple[str, str]]] = {}
    for name, bucket, detail in results:
        by_bucket.setdefault(bucket, []).append((name, detail))

    print("\n=== validation summary ===", file=sys.stderr)
    for bucket in (OK, FORMAT_MISMATCH, NEEDS_AUTH, DEAD):
        items = by_bucket.get(bucket, [])
        print(f"{bucket:16} {len(items)}", file=sys.stderr)
    empties = sum(1 for _, _, d in results if "(EMPTY)" in d)
    if empties:
        print(
            f"  (of OK, {empties} returned 0 entities — verify they're not stale)",
            file=sys.stderr,
        )

    # Detail the non-OK ones so a human can act on them.
    for bucket in (FORMAT_MISMATCH, NEEDS_AUTH, DEAD):
        items = by_bucket.get(bucket, [])
        if items:
            print(f"\n--- {bucket} ---", file=sys.stderr)
            for name, detail in sorted(items):
                print(f"  {name}: {detail}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--candidates", type=Path, default=Path("config/feeds.candidates.yaml")
    )
    p.add_argument(
        "--out", type=Path, default=Path("config/feeds.candidates.validated.yaml")
    )
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--timeout", type=int, default=15, help="per-request timeout (s)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw, feeds = load_candidates(args.candidates)
    logger.info("validating %d candidate feeds", len(feeds))
    results = asyncio.run(validate_all(feeds, args.concurrency, args.timeout))

    report(results)
    ok_names = {name for name, bucket, _ in results if bucket == OK}
    n_agencies = write_validated(raw, ok_names, args.out)
    print(
        f"\n[validate] {len(ok_names)}/{len(feeds)} feeds OK across {n_agencies} "
        f"agencies -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
