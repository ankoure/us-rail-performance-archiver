"""Per-agency S3 cost report for rail-performance-archiver.

Scans the cold and hot S3 buckets by feed prefix to compute actual storage
bytes and object counts. Landing bucket costs are estimated from poll intervals
rather than listed (too many small objects to enumerate efficiently).

Applies AWS US-East-1 pricing as of PRICING_DATE and prints a table grouped
by agency with subtotals and a grand total. Pass --csv to also write raw
numbers to a spreadsheet-friendly file.

Note: this is a point-in-time snapshot. Re-run periodically (or after onboarding
new feeds) to get an up-to-date picture.

Examples:

    # Quick scan for one agency (~10 S3 calls)
    uv run python scripts/s3_cost_report.py --agency BART --profile KourePowerUser

    # Full scan of all feeds
    uv run python scripts/s3_cost_report.py --profile KourePowerUser

    # Write CSV alongside terminal output
    uv run python scripts/s3_cost_report.py --profile KourePowerUser --csv /tmp/cost.csv

    # Adjust the assumed average raw message size (default 50 KB)
    uv run python scripts/s3_cost_report.py --avg-msg-bytes 25000
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import os
import boto3
import subprocess
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.loader import load_config
from archiver.config import ArchiverConfig

# ── Pricing constants (US-East-1, as of 2026-06-20) ──────────────────────────
PRICING_DATE = "2026-06-20"
DEEP_ARCHIVE_GB_MONTH = 0.00099  # Glacier Deep Archive storage per GB/month
IT_GB_MONTH = 0.023  # Intelligent-Tiering (frequent-access tier) per GB/month
IT_MONITORING_PER_1K = 0.0025  # IT monitoring fee per 1,000 objects/month
STANDARD_GB_MONTH = 0.023  # Standard storage (landing bucket) per GB/month
PUT_COST_EACH = 0.000005  # $5.00 per million PUT requests
GET_COST_EACH = 0.0000004  # $0.40 per million GET requests
_GB = 1 << 30


@dataclass
class FeedCost:
    cold_bytes: int = 0
    cold_objects: int = 0
    hot_bytes: int = 0
    hot_objects: int = 0
    landing_est_bytes: float = 0.0
    put_count: float = 0.0
    get_count: float = 0.0

    @property
    def cold_usd(self) -> float:
        return (self.cold_bytes / _GB) * DEEP_ARCHIVE_GB_MONTH

    @property
    def hot_usd(self) -> float:
        storage = (self.hot_bytes / _GB) * IT_GB_MONTH
        monitoring = (self.hot_objects / 1_000) * IT_MONITORING_PER_1K
        return storage + monitoring

    @property
    def landing_usd(self) -> float:
        return (self.landing_est_bytes / _GB) * STANDARD_GB_MONTH

    @property
    def requests_usd(self) -> float:
        return self.put_count * PUT_COST_EACH + self.get_count * GET_COST_EACH

    @property
    def total_usd(self) -> float:
        return self.cold_usd + self.hot_usd + self.landing_usd + self.requests_usd

    def add(self, other: "FeedCost") -> None:
        self.cold_bytes += other.cold_bytes
        self.cold_objects += other.cold_objects
        self.hot_bytes += other.hot_bytes
        self.hot_objects += other.hot_objects
        self.landing_est_bytes += other.landing_est_bytes
        self.put_count += other.put_count
        self.get_count += other.get_count


# ── S3 helpers ────────────────────────────────────────────────────────────────


def _sum_bytes_under_prefix(client, bucket: str, prefix: str) -> tuple[int, int]:
    """Return (total_bytes, object_count) for all objects under prefix."""
    paginator = client.get_paginator("list_objects_v2")
    total_bytes = total_objects = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            total_bytes += obj["Size"]
            total_objects += 1
    return total_bytes, total_objects


def _hot_kind_prefixes(client, bucket: str, root_prefix: str) -> list[str]:
    """Discover per-kind prefixes in the hot bucket.

    The hot bucket uses kind-first partitioning:
      vehicles/feed=.../...
      metrics/route_day/feed=.../...

    We use list_objects_v2 with Delimiter='/' to enumerate kind 'directories'
    cheaply, then recurse one level into metrics/ to expand its sub-kinds.
    """
    resp = client.list_objects_v2(Bucket=bucket, Prefix=root_prefix, Delimiter="/")
    prefixes: list[str] = []
    metrics_prefix = f"{root_prefix}metrics/"
    for cp in resp.get("CommonPrefixes", []):
        p = cp["Prefix"]
        if p == metrics_prefix:
            sub = client.list_objects_v2(Bucket=bucket, Prefix=p, Delimiter="/")
            prefixes.extend(scp["Prefix"] for scp in sub.get("CommonPrefixes", []))
        else:
            prefixes.append(p)
    return prefixes


def _scan_cold(client, config: ArchiverConfig, feed_name: str) -> tuple[int, int]:
    prefix = f"{config.s3.cold_prefix}{feed_name}/"
    return _sum_bytes_under_prefix(client, config.s3.cold_bucket, prefix)


def _scan_hot(
    client, config: ArchiverConfig, feed_name: str, kind_prefixes: list[str]
) -> tuple[int, int]:
    total_bytes = total_objects = 0
    for kind_prefix in kind_prefixes:
        b, o = _sum_bytes_under_prefix(
            client, config.s3.hot_bucket, f"{kind_prefix}feed={feed_name}/"
        )
        total_bytes += b
        total_objects += o
    return total_bytes, total_objects


def _estimate_landing(
    poll_interval_s: int,
    avg_msg_bytes: int,
    window_s: int,
    merge_to_hourly: bool = False,
) -> tuple[float, float, float]:
    """Estimate landing bucket usage over the 30-day lifecycle window.

    Returns (est_bytes_stored, put_count, get_count).

    Storage is the peak bytes in the bucket (30 days × daily writes). S3
    bills on time-weighted GB-months, so the real cost is ~half this; the
    estimate errs on the high side intentionally.

    When merge_to_hourly=True, the uploader merges 5-min window files into
    one hourly .bin + .jsonl before uploading, so S3 sees 24 objects/feed/day
    instead of 2 × (86400/window_s).
    """
    polls_per_day = 86400 / poll_interval_s
    if merge_to_hourly:
        objects_per_day = 2 * 24  # one .bin + one .jsonl per hour
    else:
        objects_per_day = 2 * (86400 / window_s)  # one .bin + one .jsonl per window
    est_bytes = polls_per_day * avg_msg_bytes * 30
    put_count = objects_per_day * 30  # 30 days of landing writes
    get_count = objects_per_day * 30  # rollup reads each object once
    return est_bytes, put_count, get_count


# ── Formatting ────────────────────────────────────────────────────────────────


def _fmt(amount: float) -> str:
    if amount == 0.0:
        return "$0.00"
    if amount < 0.01:
        return "<$0.01"
    if amount >= 1_000:
        return f"${amount:,.2f}"
    return f"${amount:.2f}"


def _print_report(rows: list[tuple[str, str, FeedCost]]) -> None:
    A, F, C = 22, 28, 10  # column widths: agency, feed, cost

    header = (
        f"{'Agency':<{A}}  {'Feed':<{F}}"
        f"  {'Cold/mo':>{C}}  {'Hot/mo':>{C}}"
        f"  {'Landing†':>{C}}  {'Requests':>{C}}  {'Total/mo':>{C}}"
    )
    sep = "─" * len(header)

    def cost_cols(fc: FeedCost) -> str:
        return (
            f"  {_fmt(fc.cold_usd):>{C}}  {_fmt(fc.hot_usd):>{C}}"
            f"  {_fmt(fc.landing_usd):>{C}}  {_fmt(fc.requests_usd):>{C}}"
            f"  {_fmt(fc.total_usd):>{C}}"
        )

    print(header)
    print(sep)

    current_agency: str | None = None
    agency_acc = FeedCost()
    grand = FeedCost()

    def flush_agency(name: str, acc: FeedCost) -> None:
        label = f"  → {name} total"
        print(f"{label:<{A + 2 + F}}{cost_cols(acc)}")
        print()

    for agency_name, feed_name, fc in rows:
        if agency_name != current_agency:
            if current_agency is not None:
                flush_agency(current_agency, agency_acc)
            current_agency = agency_name
            agency_acc = FeedCost()

        print(f"{agency_name:<{A}}  {feed_name:<{F}}{cost_cols(fc)}")
        agency_acc.add(fc)
        grand.add(fc)

    if current_agency is not None:
        flush_agency(current_agency, agency_acc)

    print(sep)
    print(f"{'GRAND TOTAL':<{A + 2 + F}}{cost_cols(grand)}")
    print()
    print(
        "† Landing estimate: polls/day × avg_msg_bytes × 30 days (peak, not time-weighted)"
    )


def _write_csv(rows: list[tuple[str, str, FeedCost]], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "agency",
                "feed",
                "cold_bytes",
                "cold_objects",
                "cold_usd",
                "hot_bytes",
                "hot_objects",
                "hot_usd",
                "landing_est_bytes",
                "landing_usd",
                "put_count",
                "get_count",
                "requests_usd",
                "total_usd",
            ]
        )
        for agency_name, feed_name, fc in rows:
            w.writerow(
                [
                    agency_name,
                    feed_name,
                    fc.cold_bytes,
                    fc.cold_objects,
                    f"{fc.cold_usd:.6f}",
                    fc.hot_bytes,
                    fc.hot_objects,
                    f"{fc.hot_usd:.6f}",
                    int(fc.landing_est_bytes),
                    f"{fc.landing_usd:.6f}",
                    int(fc.put_count),
                    int(fc.get_count),
                    f"{fc.requests_usd:.6f}",
                    f"{fc.total_usd:.6f}",
                ]
            )


# ── Credentials ──────────────────────────────────────────────────────────────


def _resolve_aws_credentials(profile: str | None) -> tuple[str, str, str | None]:
    """Shell out to the AWS CLI to resolve SSO/credential_process profiles.

    botocore needs `botocore[crt]` to handle SSO profiles (e.g. KourePowerUser)
    natively. The system `aws` CLI resolves them fine, so we delegate to it and
    hand the frozen keys directly to boto3 — same approach as feed_quality.py.
    """
    env = dict(os.environ)
    if profile:
        env["AWS_PROFILE"] = profile
    try:
        out = subprocess.run(
            ["aws", "configure", "export-credentials", "--format", "process"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
    except FileNotFoundError:
        raise SystemExit(
            "the `aws` CLI is required to resolve S3 credentials; install it"
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            "could not resolve AWS credentials; set AWS_PROFILE (e.g. KourePowerUser) "
            f"or pass --profile.\n{e.stderr.strip()}"
        )
    creds = json.loads(out)
    return creds["AccessKeyId"], creds["SecretAccessKey"], creds.get("SessionToken")


# ── Main ──────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", type=Path, default=Path("config/feeds.yaml"))
    p.add_argument(
        "--profile", default=None, help="AWS profile name (or set AWS_PROFILE env var)"
    )
    p.add_argument(
        "--avg-msg-bytes",
        type=int,
        default=50_000,
        help="Average raw GTFS-RT message size in bytes for landing estimate "
        "(default: 50000 = 50 KB)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel S3 list workers (default: 8; higher risks rate limits)",
    )
    p.add_argument(
        "--feed", nargs="+", default=None, help="Restrict to these feed names"
    )
    p.add_argument(
        "--agency",
        nargs="+",
        default=None,
        help="Restrict to these agency IDs (e.g. BART MTA)",
    )
    p.add_argument(
        "--csv", type=Path, default=None, help="Also write results to this CSV path"
    )
    args = p.parse_args(argv)

    config = load_config(str(args.config))
    if not config.s3.enabled or not config.s3.cold_bucket or not config.s3.hot_bucket:
        print(
            "ERROR: s3.enabled=false or cold_bucket/hot_bucket not set in config.\n"
            "This script requires both buckets to be configured.",
            file=sys.stderr,
        )
        return 1

    access_key, secret_key, token = _resolve_aws_credentials(args.profile)
    client = boto3.client(
        "s3",
        region_name=config.s3.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=token,
    )

    print(
        f"Discovering kind prefixes in s3://{config.s3.hot_bucket}/{config.s3.hot_prefix}…",
        flush=True,
    )
    kind_prefixes = _hot_kind_prefixes(
        client, config.s3.hot_bucket, config.s3.hot_prefix
    )
    kind_names = [p.rstrip("/").rsplit("/", 1)[-1] for p in kind_prefixes]
    print(f"  {len(kind_prefixes)} kind(s): {', '.join(kind_names)}", flush=True)

    work = [
        (agency, feed)
        for agency in config.agencies
        for feed in agency.feeds
        if (args.agency is None or agency.agency_id in args.agency)
        and (args.feed is None or feed.name in args.feed)
    ]
    if not work:
        print("No feeds matched the given --agency/--feed filters.", file=sys.stderr)
        return 1

    print(
        f"Scanning {len(work)} feed(s) across "
        f"s3://{config.s3.cold_bucket} and s3://{config.s3.hot_bucket}…",
        flush=True,
    )

    def scan_one(agency_feed):
        agency, feed = agency_feed
        cold_b, cold_o = _scan_cold(client, config, feed.name)
        hot_b, hot_o = _scan_hot(client, config, feed.name, kind_prefixes)
        poll_interval = feed.poll_interval_seconds or 30
        est_bytes, put_count, get_count = _estimate_landing(
            poll_interval,
            args.avg_msg_bytes,
            config.writer.window_seconds,
            config.writer.merge_to_hourly,
        )
        return (
            agency.name,
            feed.name,
            FeedCost(
                cold_bytes=cold_b,
                cold_objects=cold_o,
                hot_bytes=hot_b,
                hot_objects=hot_o,
                landing_est_bytes=est_bytes,
                put_count=put_count,
                get_count=get_count,
            ),
        )

    results: list[tuple[str, str, FeedCost]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(scan_one, w): w for w in work}
        done = 0
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if done % 25 == 0 or done == len(work):
                print(f"  {done}/{len(work)} feeds scanned…", flush=True)

    results.sort(key=lambda r: (r[0], r[1]))

    print(f"\nS3 cost report — pricing as of {PRICING_DATE} (US-East-1)")
    print(
        f"Feeds: {len(results)}  |  avg_msg_bytes: {args.avg_msg_bytes:,}"
        f"  |  cold: {config.s3.cold_bucket}  |  hot: {config.s3.hot_bucket}"
    )
    print()
    _print_report(results)

    if args.csv:
        _write_csv(results, args.csv)
        print(f"CSV written → {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
