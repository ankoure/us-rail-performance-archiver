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
from pathlib import Path
import os
import boto3
import subprocess
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.loader import load_config
from pipeline.s3_agency_scan import (
    PRICING_DATE,
    FeedCost,
    hot_kind_prefixes,
    scan_agencies,
)

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
    kind_prefixes = hot_kind_prefixes(
        client, config.s3.hot_bucket, config.s3.hot_prefix
    )
    kind_names = [p.rstrip("/").rsplit("/", 1)[-1] for p in kind_prefixes]
    print(f"  {len(kind_prefixes)} kind(s): {', '.join(kind_names)}", flush=True)

    work_count = sum(
        1
        for agency in config.agencies
        for feed in agency.feeds
        if (args.agency is None or agency.agency_id in args.agency)
        and (args.feed is None or feed.name in args.feed)
    )
    if not work_count:
        print("No feeds matched the given --agency/--feed filters.", file=sys.stderr)
        return 1

    print(
        f"Scanning {work_count} feed(s) across "
        f"s3://{config.s3.cold_bucket} and s3://{config.s3.hot_bucket}…",
        flush=True,
    )

    def report_progress(done: int, total: int) -> None:
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total} feeds scanned…", flush=True)

    results = scan_agencies(
        client,
        config,
        kind_prefixes=kind_prefixes,
        agencies=args.agency,
        feeds=args.feed,
        avg_msg_bytes=args.avg_msg_bytes,
        workers=args.workers,
        progress=report_progress,
    )

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
