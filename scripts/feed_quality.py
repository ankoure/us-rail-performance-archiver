"""Report per-feed field coverage in the silver `vehicles` parquet.

For each feed it scans the curated vehicle rows and reports the share that have
`route_id`, `direction_id`, `stop_id` and `current_status` (stop status) set —
the fields the gold tier depends on:

  - route_id + stop_id  : required to bucket Visits into the marts at all
  - current_status      : enables precise STOPPED_AT dwell detection; without it
                          dwells fall back to coarser position-based runs
  - direction_id        : needed for the per-direction split (else one null
                          bucket; GTFS backfill can recover it later)

Feeds publish one of two silver column schemes (dotted protobuf paths like
`vehicle.trip.route_id`, or flat `route_id`); this resolves either, and the
detected scheme is part of the report.

The curated tree can be read locally or from the S3 hot bucket — `--s3` reads
the same `vehicles/feed=.../year=.../month=.../day=.../data.parquet` layout the
shipper uploads, resolving bucket/prefix/region from `config/feeds.yaml`.
Credentials are resolved via boto3 — set `AWS_PROFILE=KourePowerUser` (or pass
`--profile`); the rest of the archiver authenticates the same way.

Examples:

    uv run python scripts/feed_quality.py                 # local curated/, all feeds, all days
    uv run python scripts/feed_quality.py --s3            # read the S3 hot bucket instead
    uv run python scripts/feed_quality.py --s3 --day 2026-05-24
    uv run python scripts/feed_quality.py --s3 --feed wmata-vehicles caltrain-vehicles
    uv run python scripts/feed_quality.py --s3 --bucket my-bucket --prefix some/prefix/
    uv run python scripts/feed_quality.py --csv quality.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyarrow import fs as pafs

# Allow `from archiver...` imports when run as `python scripts/feed_quality.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# logical field -> candidate column names (dotted protobuf path, or flat).
_FIELDS = {
    "route": ["vehicle.trip.route_id", "route_id"],
    "dir": ["vehicle.trip.direction_id", "direction_id"],
    "stop": ["vehicle.stop_id", "stop_id"],
    "status": ["vehicle.current_status", "current_status"],
}


@dataclass
class FeedReport:
    feed: str
    days: int
    rows: int
    set_counts: dict[str, int]  # logical field -> rows with it set
    scheme: str  # "dotted" | "flat" | "mixed" | "—"

    def pct(self, field: str) -> float | None:
        if self.rows == 0:
            return None
        return 100.0 * self.set_counts[field] / self.rows

    @property
    def notes(self) -> str:
        """Actionable analysis implications, most important first."""
        if self.rows == 0:
            return "no rows"
        out: list[str] = []
        if (self.pct("stop") or 0) < 1:
            out.append("no stop_id → use --source trip-updates")
        elif (self.pct("status") or 0) < 1:
            out.append("no current_status → position-based dwell")
        if (self.pct("route") or 0) < 1:
            out.append("no route_id")
        elif (self.pct("dir") or 0) < 1:
            out.append("no direction → GTFS backfill")
        return "; ".join(out)


def _resolve(field: str, columns: set[str]) -> str | None:
    for candidate in _FIELDS[field]:
        if candidate in columns:
            return candidate
    return None


def _count_set(col: pa.ChunkedArray) -> int:
    """Rows that are non-null (and non-empty, for strings)."""
    valid = pc.is_valid(col)
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
        valid = pc.and_kleene(valid, pc.not_equal(col, pa.scalar("", col.type)))
    return pc.sum(pc.cast(valid, pa.int64())).as_py() or 0


def _stats_set_count(md: "pq.FileMetaData", col_index: int) -> tuple[bool, int]:
    """Set-row count for one column from footer stats, without reading data.

    Returns (exact, count). exact is False — caller must read the column — when
    a row group lacks a null_count, or a string column's min is "" (empty
    strings would be miscounted as set, since stats only track nulls).
    """
    nulls = 0
    for i in range(md.num_row_groups):
        st = md.row_group(i).column(col_index).statistics
        if st is None or not getattr(st, "has_null_count", False):
            return False, 0
        if getattr(st, "has_min_max", False) and st.min in ("", b""):
            return False, 0  # empties present; null_count alone would overcount
        nulls += st.null_count
    return True, md.num_rows - nulls


def _path_day(path: str) -> dt.date | None:
    """Parse the day from `.../year=YYYY/month=M/day=D/...` path segments."""
    parts: dict[str, int] = {}
    for seg in path.split("/"):
        key, sep, val = seg.partition("=")
        if sep and key in ("year", "month", "day"):
            try:
                parts[key] = int(val)
            except ValueError:
                return None
    if set(parts) != {"year", "month", "day"}:
        return None
    try:
        return dt.date(parts["year"], parts["month"], parts["day"])
    except ValueError:
        return None


class CuratedStore:
    """Reads the curated `vehicles` tree from a local dir or the S3 hot bucket.

    Both back ends go through pyarrow's filesystem interface, so discovery
    (`get_file_info` over a `FileSelector`) and reads (`pq.ParquetFile(... ,
    filesystem=fs)`) are identical regardless of where the parquet lives.
    """

    def __init__(self, filesystem: pafs.FileSystem, root: str, label: str, *, refresh=None):
        self.fs = filesystem
        self.root = root.rstrip("/")
        self.label = label  # human-readable source, for error messages
        # Rebuilds the filesystem with fresh credentials, or None for local. A
        # full all-days S3 scan can outlive the exported session token; pyarrow
        # surfaces that as an opaque GetObject HTTP 400. We re-resolve and retry
        # once, caching the fresh fs back so only the first post-expiry call pays.
        self._refresh = refresh

    def _attempt(self, fn):
        try:
            return fn()
        except OSError:
            if self._refresh is None:
                raise
            self.fs = self._refresh()
            return fn()

    @property
    def _vehicles_root(self) -> str:
        return f"{self.root}/vehicles"

    def feeds(self) -> list[str]:
        def _list():
            sel = pafs.FileSelector(self._vehicles_root, allow_not_found=True)
            return [
                info.base_name[len("feed=") :]
                for info in self.fs.get_file_info(sel)
                if info.type == pafs.FileType.Directory
                and info.base_name.startswith("feed=")
            ]
        return sorted(self._attempt(_list))

    def feed_days(self, feed: str, day: dt.date | None) -> dict[dt.date, list[str]]:
        """Map each day-partition to its parquet file paths for one feed."""
        def _list():
            sel = pafs.FileSelector(
                f"{self._vehicles_root}/feed={feed}", recursive=True, allow_not_found=True
            )
            days: dict[dt.date, list[str]] = {}
            for info in self.fs.get_file_info(sel):
                if info.type != pafs.FileType.File or not info.path.endswith(".parquet"):
                    continue
                d = _path_day(info.path)
                if d is None or (day is not None and d != day):
                    continue
                days.setdefault(d, []).append(info.path)
            return days
        return self._attempt(_list)

    def scan_parquet(
        self, path: str, fields: dict[str, list[str]]
    ) -> tuple[int, dict[str, str | None], dict[str, int]]:
        """Count set rows per field in one parquet, reading as little as possible.

        Returns (num_rows, {field: resolved column or None}, {field: set count}).
        The set count comes from the footer's per-column `null_count` statistics
        — a metadata-only read of tens of KB, vs downloading MBs of column data,
        which is the difference between a full S3 scan finishing and timing out.
        We fall back to reading a single column's data only when stats can't give
        an exact answer: a string column whose `min` is "" (empties exist, which
        `null_count` wouldn't subtract) or a column missing stats entirely.
        """
        def _do():
            pf = pq.ParquetFile(path, filesystem=self.fs)
            md = pf.metadata
            names = pf.schema_arrow.names
            index = {n: i for i, n in enumerate(names)}
            resolved = {f: _resolve(f, set(names)) for f in fields}

            counts: dict[str, int] = {}
            need_read: list[tuple[str, str]] = []
            for field, col in resolved.items():
                if col is None:
                    counts[field] = 0
                    continue
                exact, n = _stats_set_count(md, index[col])
                if exact:
                    counts[field] = n
                else:
                    need_read.append((field, col))

            if need_read:
                table = pf.read(columns=sorted({c for _, c in need_read}))
                for field, col in need_read:
                    counts[field] = _count_set(table.column(col))
            return md.num_rows, resolved, counts
        return self._attempt(_do)


def build_store(args: argparse.Namespace) -> CuratedStore:
    if not (args.s3 or args.bucket):
        return CuratedStore(
            pafs.LocalFileSystem(), str(args.curated_dir), f"{args.curated_dir}/vehicles"
        )

    bucket, prefix, region = args.bucket, args.prefix, args.region
    if bucket is None or prefix is None or region is None:
        from archiver.loader import load_config

        s3 = load_config(str(args.config)).s3
        bucket = bucket or s3.hot_bucket
        prefix = s3.hot_prefix if prefix is None else prefix
        region = region or s3.region
    if not bucket:
        raise SystemExit(f"no hot_bucket configured in {args.config}; pass --bucket")

    def make_fs() -> pafs.S3FileSystem:
        access_key, secret_key, token = _resolve_aws_credentials(args.profile)
        return pafs.S3FileSystem(
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            session_token=token,
        )

    root = f"{bucket}/{prefix}".rstrip("/")
    return CuratedStore(make_fs(), root, f"s3://{root}/vehicles", refresh=make_fs)


def _resolve_aws_credentials(profile: str | None) -> tuple[str, str, str | None]:
    """Resolve credentials and hand the frozen keys to pyarrow ourselves.

    pyarrow's bundled C++ AWS SDK can't resolve config-only profiles (SSO,
    credential_process, the `login_session` profiles here), and the venv's
    botocore needs `botocore[crt]` for them — so both self-resolve to ACCESS
    DENIED even when `aws s3 ls` works. The system `aws` CLI does resolve them,
    so we shell out to it to vend the already-resolved keys.
    """
    import json
    import subprocess

    env = dict(os.environ)
    if profile:
        env["AWS_PROFILE"] = profile
    try:
        out = subprocess.run(
            ["aws", "configure", "export-credentials", "--format", "process"],
            capture_output=True, text=True, env=env, check=True,
        ).stdout
    except FileNotFoundError:
        raise SystemExit("the `aws` CLI is required to resolve S3 credentials; install it")
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            "could not resolve AWS credentials; set AWS_PROFILE (e.g. "
            f"KourePowerUser) or pass --profile.\n{e.stderr.strip()}"
        )
    creds = json.loads(out)
    return creds["AccessKeyId"], creds["SecretAccessKey"], creds.get("SessionToken")


def scan_feed(feed: str, store: CuratedStore, day: dt.date | None) -> FeedReport:
    rows = 0
    set_counts = {f: 0 for f in _FIELDS}
    schemes: set[str] = set()
    days_seen: set[dt.date] = set()

    for d, paths in store.feed_days(feed, day).items():
        for path in sorted(paths):
            num_rows, resolved, counts = store.scan_parquet(path, _FIELDS)
            rows += num_rows
            days_seen.add(d)
            for field in _FIELDS:
                set_counts[field] += counts[field]
            # Record the scheme from the route column's resolved name.
            route_col = resolved["route"]
            if route_col is not None:
                schemes.add("dotted" if "." in route_col else "flat")

    scheme = "—"
    if len(schemes) == 1:
        scheme = next(iter(schemes))
    elif len(schemes) > 1:
        scheme = "mixed"
    return FeedReport(feed, len(days_seen), rows, set_counts, scheme)


def _fmt_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:5.1f}%"


def print_table(reports: list[FeedReport]) -> None:
    name_w = max((len(r.feed) for r in reports), default=4)
    header = (
        f"{'feed':<{name_w}}  {'days':>4}  {'rows':>11}  "
        f"{'route':>7}  {'dir':>7}  {'stop':>7}  {'status':>7}  {'scheme':<6}  notes"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        print(
            f"{r.feed:<{name_w}}  {r.days:>4}  {r.rows:>11,}  "
            f"{_fmt_pct(r.pct('route')):>7}  {_fmt_pct(r.pct('dir')):>7}  "
            f"{_fmt_pct(r.pct('stop')):>7}  {_fmt_pct(r.pct('status')):>7}  "
            f"{r.scheme:<6}  {r.notes}"
        )


def write_csv(reports: list[FeedReport], path: Path) -> None:
    with path.open("w", newline="") as fd:
        w = csv.writer(fd)
        w.writerow(
            ["feed", "days", "rows", "route_pct", "dir_pct", "stop_pct",
             "status_pct", "scheme", "notes"]
        )
        for r in reports:
            w.writerow(
                [
                    r.feed,
                    r.days,
                    r.rows,
                    f"{r.pct('route'):.1f}" if r.pct("route") is not None else "",
                    f"{r.pct('dir'):.1f}" if r.pct("dir") is not None else "",
                    f"{r.pct('stop'):.1f}" if r.pct("stop") is not None else "",
                    f"{r.pct('status'):.1f}" if r.pct("status") is not None else "",
                    r.scheme,
                    r.notes,
                ]
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed", nargs="+", default=None, help="Feeds to scan (default: all).")
    p.add_argument(
        "--day",
        type=dt.date.fromisoformat,
        help="Restrict to one day (YYYY-MM-DD). Default: every day available.",
    )
    p.add_argument("--curated-dir", type=Path, default=Path("curated"))
    p.add_argument(
        "--s3",
        action="store_true",
        help="Read the S3 hot bucket instead of the local curated dir.",
    )
    p.add_argument(
        "--bucket", default=None, help="S3 bucket (default: s3.hot_bucket from --config)."
    )
    p.add_argument(
        "--prefix", default=None, help="S3 key prefix (default: s3.hot_prefix from --config)."
    )
    p.add_argument(
        "--region", default=None, help="AWS region (default: s3.region from --config)."
    )
    p.add_argument(
        "--profile",
        default=None,
        help="AWS profile (default: AWS_PROFILE env / default credential chain).",
    )
    p.add_argument("--config", type=Path, default=Path("config/feeds.yaml"))
    p.add_argument("--csv", type=Path, default=None, help="Also write the report here.")
    p.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel feed scans (footer reads are latency-bound; default 8).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = build_store(args)
    feeds = args.feed if args.feed else store.feeds()
    if not feeds:
        print(f"no vehicle feeds found under {store.label}", file=sys.stderr)
        return 1

    # Each feed scan is independent and dominated by S3 round-trip latency, so a
    # thread pool collapses the wall time of an all-feeds scan. ex.map preserves
    # feed order for a stable report.
    if args.workers > 1 and len(feeds) > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            reports = list(ex.map(lambda f: scan_feed(f, store, args.day), feeds))
    else:
        reports = [scan_feed(f, store, args.day) for f in feeds]
    print_table(reports)
    if args.csv:
        write_csv(reports, args.csv)
        print(f"\nwrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
