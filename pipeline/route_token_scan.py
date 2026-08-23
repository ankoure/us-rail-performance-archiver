"""Scan archived RT data for route tokens that don't resolve to a known GTFS
route_id, and report which ones still need a manual entry in PostGIS'
route_token_overrides table.

Most feeds are GTFS-RT protobuf, where the RT payload's route_id is *assumed*
to already be a canonical GTFS static route_id -- this script checks that
assumption empirically instead of trusting it, by diffing every distinct
token actually observed in the curated data against the feed's current
gtfs_routes mart (see pipeline/gtfs.py). Tokens that match need nothing.

A handful of non-protobuf JSON feeds (MARTA, MWRTA/CCRTA, BRTA, MEVA,
GATRA/LRTA/WRTA, Vineyard VTA, FRTA/MART) already run their raw token through
a per-agency resolver in analysis/normalize.py before landing in
curated/normalized_vehicles/ -- for those, this script trusts an already
`resolved`/`passthrough` row and only flags tokens marked
`route_unresolved`/`no_route`, rather than re-deriving resolution logic here.

Read-only: reads curated Parquet and (to see which unresolved tokens already
have a human-supplied mapping and can be skipped) PostGIS' route_token_overrides.
Never writes anything -- adding an override is a manual act, same as editing
a shape in route_shape_edits (see pipeline/sql/postgis_schema.sql).

--curated-dir accepts either a local path (default) or an s3://bucket/prefix
URI -- e.g. the hot bucket dashboard/api already reads from (see
dashboard/api/services/data.py's s3: block in config/feeds.yaml), for
scanning data that's been shipped off the archiver box and may no longer be
on local disk. Uses pyarrow.dataset with hive partitioning either way, so
the same scan logic runs unmodified against both.

Examples:

    # every day on disk for a protobuf feed
    uv run --group postgis python pipeline/route_token_scan.py --feed septa-rail-vehicles --all-days

    # a JSON-normalized feed, one day
    uv run --group postgis python pipeline/route_token_scan.py --feed marta-traindata --day 2026-06-09

    # scan the hot bucket instead of local disk
    uv run --group postgis python pipeline/route_token_scan.py --feed septa-rail-vehicles \\
        --all-days --curated-dir s3://my-hot-bucket/some/prefix/
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import Counter
from pathlib import Path

import boto3
import psycopg
import pyarrow.compute as pc
import pyarrow.dataset as pds
import pyarrow.fs as pafs
from dotenv import load_dotenv

# Make the repo root importable when run as `python pipeline/route_token_scan.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

_SOURCE_COLUMN = {
    "vehicles": "vehicle.trip.route_id",
    "trip-updates": "trip_update.trip.route_id",
}
_SOURCE_SUBDIR = {"vehicles": "vehicles", "trip-updates": "trip_updates"}

# analysis/normalize.py's resolution_status vocabulary -- statuses that mean
# "already handled by an existing per-agency resolver, nothing to flag".
_ALREADY_RESOLVED = {"resolved", "passthrough"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed", required=True, help="Feed name (as in config/feeds.yaml).")
    p.add_argument(
        "--day",
        nargs="+",
        type=dt.date.fromisoformat,
        default=[],
        help="One or more days (YYYY-MM-DD) to scan.",
    )
    p.add_argument(
        "--all-days",
        action="store_true",
        help="Scan every day on disk/in the bucket for this feed (ignores --day).",
    )
    p.add_argument(
        "--source",
        choices=["vehicles", "trip-updates"],
        default="vehicles",
        help="Which curated dataset to scan route tokens from (default: vehicles).",
    )
    p.add_argument(
        "--curated-dir",
        default="data/curated",
        help="Curated root: a local path (default) or an s3://bucket/prefix URI.",
    )
    p.add_argument(
        "--database-url",
        default=os.environ.get("POSTGIS_DATABASE_URL"),
        help="PostGIS connection string, to check existing overrides. Defaults "
        "to $POSTGIS_DATABASE_URL. Omit to skip that check (everything "
        "unmatched is reported, even if already overridden).",
    )
    args = p.parse_args(argv)
    if not args.day and not args.all_days:
        p.error("provide --day YYYY-MM-DD [...] or --all-days")
    return args


def resolve_root(curated_dir: str) -> tuple[str, pafs.FileSystem | None]:
    """A local path (filesystem=None -- pyarrow.dataset uses the local FS) or
    an s3://bucket/prefix URI resolved to a credentialed S3FileSystem the
    same way dashboard/api/services/data.py does: boto3's own credential
    resolution (AWS_PROFILE locally, EC2 instance profile in prod), since
    pyarrow's built-in chain doesn't reliably honor AWS_PROFILE.
    """
    if not curated_dir.startswith("s3://"):
        return curated_dir, None
    rest = curated_dir[len("s3://") :].rstrip("/")
    bucket = rest.split("/", 1)[0]
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    filesystem = pafs.S3FileSystem(
        region=pafs.resolve_s3_region(bucket),
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        session_token=creds.token,
    )
    return rest, filesystem


def _open_dataset(
    root: str, filesystem: pafs.FileSystem | None, subdir: str, feed: str
) -> pds.Dataset | None:
    """The hive-partitioned dataset at {root}/{subdir}/feed={feed}, or None
    if that path doesn't exist or has no data -- callers treat both as "not
    populated yet", not an error."""
    path = f"{root.rstrip('/')}/{subdir}/feed={feed}"
    try:
        dataset = pds.dataset(
            path, filesystem=filesystem, format="parquet", partitioning="hive"
        )
        if dataset.count_rows() == 0:
            return None
    except (FileNotFoundError, OSError):
        return None
    return dataset


def _day_filter(days: list[dt.date] | None):
    """A pyarrow compute expression matching any of `days` on the dataset's
    hive-partitioned year/month/day columns, or None (no filter -- every
    row) when `days` is empty."""
    if not days:
        return None
    expr = None
    for d in days:
        clause = (
            (pc.field("year") == d.year)
            & (pc.field("month") == d.month)
            & (pc.field("day") == d.day)
        )
        expr = clause if expr is None else (expr | clause)
    return expr


def scan_raw_tokens(
    root: str,
    filesystem: pafs.FileSystem | None,
    source: str,
    feed: str,
    days: list[dt.date] | None,
) -> Counter:
    """Distinct route-token counts from the raw (protobuf) curated table."""
    dataset = _open_dataset(root, filesystem, _SOURCE_SUBDIR[source], feed)
    if dataset is None:
        return Counter()
    column = _SOURCE_COLUMN[source]
    table = dataset.to_table(columns=[column], filter=_day_filter(days))
    return Counter(tok for tok in table.column(column).to_pylist() if tok)


def scan_normalized_tokens(
    root: str, filesystem: pafs.FileSystem | None, feed: str, days: list[dt.date] | None
) -> Counter:
    """Distinct raw_route counts among already-unresolved rows in
    curated/normalized_vehicles -- rows analysis/normalize.py's per-agency
    resolvers couldn't match."""
    dataset = _open_dataset(root, filesystem, "normalized_vehicles", feed)
    if dataset is None:
        return Counter()
    table = dataset.to_table(
        columns=["raw_route", "resolution_status"], filter=_day_filter(days)
    )
    counts: Counter = Counter()
    for token, status in zip(
        table.column("raw_route").to_pylist(),
        table.column("resolution_status").to_pylist(),
    ):
        if token and status not in _ALREADY_RESOLVED:
            counts[token] += 1
    return counts


def known_route_ids(
    root: str, filesystem: pafs.FileSystem | None, feed: str
) -> set[str] | None:
    """This feed's current static route_id set, from the latest version
    already in gtfs_routes. None if pipeline/gtfs.py hasn't been run for
    this feed yet (caller should treat every raw token as unverifiable, not
    silently treat everything as unmatched)."""
    dataset = _open_dataset(root, filesystem, "metrics/gtfs_routes", feed)
    if dataset is None:
        return None
    table = dataset.to_table(columns=["route_id", "version_slug"])
    latest = max(table.column("version_slug").to_pylist())
    filtered = table.filter(pc.field("version_slug") == latest)
    return set(filtered.column("route_id").to_pylist())


def existing_overrides(database_url: str | None, feed: str) -> set[str]:
    if not database_url:
        return set()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT raw_token FROM route_token_overrides WHERE feed = %s", (feed,)
        )
        return {row[0] for row in cur.fetchall()}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root, filesystem = resolve_root(args.curated_dir)
    days = None if args.all_days else args.day

    normalized_present = (
        _open_dataset(root, filesystem, "normalized_vehicles", args.feed) is not None
    )
    if normalized_present:
        # normalize.py's per-agency resolvers already did the matching;
        # scan_normalized_tokens only returns rows they couldn't resolve, so
        # an empty result here is a legitimate "nothing unresolved", not
        # "no data".
        flagged = scan_normalized_tokens(root, filesystem, args.feed, days)
        source_desc = "curated/normalized_vehicles (already-unresolved rows)"
    else:
        raw_counts = scan_raw_tokens(root, filesystem, args.source, args.feed, days)
        source_desc = f"curated/{_SOURCE_SUBDIR[args.source]}"
        if not raw_counts:
            print(
                f"[{args.feed}] no {args.source} data found under {args.curated_dir!r}",
                file=sys.stderr,
            )
            return 1
        route_ids = known_route_ids(root, filesystem, args.feed)
        if route_ids is None:
            print(
                f"[{args.feed}] no gtfs_routes mart found under {args.curated_dir!r} -- "
                f"run `pipeline/gtfs.py --feed {args.feed} --all-days` first",
                file=sys.stderr,
            )
            return 1
        flagged = Counter(
            {tok: n for tok, n in raw_counts.items() if tok not in route_ids}
        )

    overridden = existing_overrides(args.database_url, args.feed)
    unhandled = Counter({tok: n for tok, n in flagged.items() if tok not in overridden})

    print(
        f"[{args.feed}] scanned {source_desc}: "
        f"{len(unhandled)} token(s) need attention"
        + (
            f" ({len(flagged) - len(unhandled)} already overridden)"
            if overridden
            else ""
        )
    )
    for token, n in unhandled.most_common():
        print(f"  {token!r:20} seen {n:,}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
