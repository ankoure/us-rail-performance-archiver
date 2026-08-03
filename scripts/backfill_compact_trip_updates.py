"""One-time backfill: compact existing trip_updates partitions in the hot
bucket to one row per stop visit.

pipeline/compact_trip_updates.py handles this going forward for each day as
it's rolled up (runs against the local disk of that day's Fargate task). This
script is for the ~600 GiB already written before that existed — it doesn't
live on any task's disk anymore, only in S3, so this reads/compacts/rewrites
objects directly against the hot bucket. Reuses the exact same
compact_table() reduction, so results are identical to what the daily job
would have produced.

Defaults to a dry run: downloads and compacts each partition in memory to
report the real row/byte reduction, but never writes anything back. Pass
--apply to actually overwrite objects in place.

Examples:

    # preview total impact across every feed
    uv run python scripts/backfill_compact_trip_updates.py --profile KourePowerUser

    # preview just one feed first
    uv run python scripts/backfill_compact_trip_updates.py --feed metromn-trips --profile KourePowerUser

    # actually rewrite one feed
    uv run python scripts/backfill_compact_trip_updates.py --apply --feed metromn-trips --profile KourePowerUser

    # actually rewrite everything
    uv run python scripts/backfill_compact_trip_updates.py --apply --profile KourePowerUser
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.loader import load_config  # noqa: E402
from pipeline.compact_trip_updates import compact_table  # noqa: E402
from scripts.migrate_rt_schema import classify  # noqa: E402


def _resolve_aws_credentials(profile: str | None) -> tuple[str, str, str | None]:
    """Shell out to the AWS CLI to resolve SSO/credential_process profiles.

    botocore needs `botocore[crt]` to handle SSO profiles (e.g. KourePowerUser)
    natively. The system `aws` CLI resolves them fine, so we delegate to it and
    hand the frozen keys directly to boto3 — same approach as s3_cost_report.py.
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


def list_feeds(client, bucket: str) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    feeds = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix="trip_updates/", Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            # "trip_updates/feed=metromn-trips/" -> "metromn-trips"
            feeds.append(cp["Prefix"].split("feed=", 1)[1].rstrip("/"))
    return sorted(feeds)


def list_partitions(client, bucket: str, feed: str) -> list[str]:
    """Every trip_updates/feed=<feed>/.../data.parquet key for one feed."""
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=f"trip_updates/feed={feed}/"
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("data.parquet"):
                keys.append(obj["Key"])
    return keys


def compact_key(client, bucket: str, key: str, apply: bool) -> dict:
    """Download, compact, and (if apply) overwrite one object in place."""
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    before_bytes = len(body)
    result = {
        "key": key,
        "before_rows": 0,
        "after_rows": 0,
        "before_bytes": before_bytes,
        "after_bytes": before_bytes,
        "status": "unchanged",
    }

    schema_names = [f.name for f in pq.ParquetFile(io.BytesIO(body)).schema_arrow]
    era = classify(schema_names)
    if era != "DOTTED":
        # Predates the current column-naming convention (see
        # scripts/migrate_rt_schema.py) — compact_table's column names won't
        # match. Needs that migration run against this object first; skip
        # rather than error, so a partial-era backfill still completes cleanly.
        result["status"] = f"needs-migration:{era}"
        return result

    before = pq.ParquetFile(io.BytesIO(body)).read()
    result["before_rows"] = before.num_rows
    result["after_rows"] = before.num_rows
    if before.num_rows == 0:
        return result

    after = compact_table(before)
    result["after_rows"] = after.num_rows
    if after.num_rows == before.num_rows:
        return result

    buf = io.BytesIO()
    pq.write_table(after, buf)
    result["after_bytes"] = buf.tell()
    result["status"] = "written" if apply else "would-write"
    if apply:
        client.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())

    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", type=Path, default=Path("config/feeds.yaml"))
    p.add_argument(
        "--profile", default=None, help="AWS profile name (or set AWS_PROFILE env var)"
    )
    p.add_argument(
        "--feed", nargs="+", default=None, help="Restrict to these feed names"
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually rewrite objects in place (default: dry run, report only)",
    )
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(str(args.config))
    if not config.s3.enabled or not config.s3.hot_bucket:
        print("ERROR: s3.enabled=false or hot_bucket not set in config", file=sys.stderr)
        return 1

    if args.profile:
        # Named profile (e.g. an SSO profile like KourePowerUser) — botocore
        # can't resolve those natively, so shell out to the aws CLI, same as
        # s3_cost_report.py.
        access_key, secret_key, token = _resolve_aws_credentials(args.profile)
        client = boto3.client(
            "s3",
            region_name=config.s3.region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=token,
        )
    else:
        # No profile given: let boto3's default credential chain resolve it
        # (e.g. the ECS task role when running via run-task) — no aws CLI
        # dependency, which the container image doesn't have anyway.
        client = boto3.client("s3", region_name=config.s3.region)
    bucket = config.s3.hot_bucket

    feeds = args.feed if args.feed else list_feeds(client, bucket)
    if not feeds:
        print("no trip_updates feeds found", file=sys.stderr)
        return 0
    print(f"feeds: {len(feeds)}  |  bucket: {bucket}  |  mode: "
          f"{'APPLY' if args.apply else 'dry run'}")

    keys: list[str] = []
    for feed in feeds:
        keys.extend(list_partitions(client, bucket, feed))
    print(f"partitions: {len(keys)}", flush=True)

    total_before_rows = total_after_rows = 0
    total_before_bytes = total_after_bytes = 0
    tally: dict[str, int] = {}
    errors: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(compact_key, client, bucket, k, args.apply): k for k in keys}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001 - report and continue
                errors.append(f"{key}: {type(e).__name__}: {e}")
                continue
            total_before_rows += r["before_rows"]
            total_after_rows += r["after_rows"]
            total_before_bytes += r["before_bytes"]
            total_after_bytes += r["after_bytes"]
            tally[r["status"]] = tally.get(r["status"], 0) + 1
            done += 1
            if done % 50 == 0 or done == len(keys):
                print(f"  {done}/{len(keys)} partitions processed…", flush=True)

    print()
    for status in sorted(tally):
        print(f"  {status:<24} {tally[status]}")
    if total_before_bytes:
        pct = 100 * (1 - total_after_bytes / total_before_bytes)
        print(
            f"\nrows:  {total_before_rows:,} -> {total_after_rows:,}\n"
            f"bytes: {total_before_bytes/1e9:.2f} GB -> {total_after_bytes/1e9:.2f} GB "
            f"({pct:.1f}% smaller)"
        )
    if errors:
        print(f"\n{len(errors)} errors:", file=sys.stderr)
        for e in errors[:20]:
            print(f"  {e}", file=sys.stderr)

    if not args.apply:
        print("\n(dry run — re-run with --apply to rewrite)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
