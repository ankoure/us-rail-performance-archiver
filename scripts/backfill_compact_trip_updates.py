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
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
import pyarrow.parquet as pq
from botocore.config import Config as BotoConfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.loader import load_config  # noqa: E402
from pipeline.compact_trip_updates import compact_parquet  # noqa: E402
from scripts.migrate_rt_schema import classify  # noqa: E402

# Partitions run up to ~500 MB (metromn-trips' biggest day); a stalled
# connection on one of ThreadPoolExecutor's workers would otherwise hang that
# thread — and eventually the whole run — forever, since boto3's defaults have
# no timeout. Bounded retry for transient S3 errors too.
_BOTO_CONFIG = BotoConfig(
    connect_timeout=10,
    read_timeout=120,
    retries={"max_attempts": 3, "mode": "standard"},
)


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
    for page in paginator.paginate(Bucket=bucket, Prefix=f"trip_updates/feed={feed}/"):
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

    pf = pq.ParquetFile(io.BytesIO(body))
    era = classify([f.name for f in pf.schema_arrow])
    if era != "DOTTED":
        # Predates the current column-naming convention (see
        # scripts/migrate_rt_schema.py) — compact_table's column names won't
        # match. Needs that migration run against this object first; skip
        # rather than error, so a partial-era backfill still completes cleanly.
        result["status"] = f"needs-migration:{era}"
        return result

    if pf.metadata.num_rows == 0:
        return result

    # Row-group-at-a-time (pipeline.compact_trip_updates.compact_parquet),
    # not a single whole-table read: metromn-trips' biggest day is 70M+ rows,
    # and materializing + sorting/grouping that in one shot is what OOM-killed
    # this task even at low --workers concurrency.
    before_rows, after = compact_parquet(pf)
    result["before_rows"] = before_rows
    result["after_rows"] = after.num_rows
    if after.num_rows == before_rows:
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
    p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel S3 download/compact workers (default: 4). "
        "compact_parquet() batches its reduction (~2.6 GB measured peak on "
        "the largest known partition), but several workers landing on large "
        "feeds at once still adds up against the task's 10 GiB ceiling.",
    )
    # Internal: set by main()'s per-feed subprocess driver so the child knows
    # where to write its stats for the parent to aggregate. Not for direct use.
    p.add_argument("--result-file", type=Path, default=None, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _build_client(args: argparse.Namespace, config) -> "boto3.client":
    if args.profile:
        # Named profile (e.g. an SSO profile like KourePowerUser) — botocore
        # can't resolve those natively, so shell out to the aws CLI, same as
        # s3_cost_report.py.
        access_key, secret_key, token = _resolve_aws_credentials(args.profile)
        return boto3.client(
            "s3",
            region_name=config.s3.region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=token,
            config=_BOTO_CONFIG,
        )
    # No profile given: let boto3's default credential chain resolve it (e.g.
    # the ECS task role when running via run-task) — no aws CLI dependency,
    # which the container image doesn't have anyway.
    return boto3.client("s3", region_name=config.s3.region, config=_BOTO_CONFIG)


def _run_feeds(
    client, bucket: str, feeds: list[str], apply: bool, workers: int
) -> dict:
    """Compact every partition across `feeds` in the current process. Used
    both for a direct --feed run and as what each per-feed child subprocess
    does for its one assigned feed."""
    keys: list[str] = []
    for feed in feeds:
        keys.extend(list_partitions(client, bucket, feed))
    print(f"partitions: {len(keys)}", flush=True)

    total_before_rows = total_after_rows = 0
    total_before_bytes = total_after_bytes = 0
    tally: dict[str, int] = {}
    errors: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(compact_key, client, bucket, k, apply): k for k in keys}
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

    return {
        "partitions": len(keys),
        "before_rows": total_before_rows,
        "after_rows": total_after_rows,
        "before_bytes": total_before_bytes,
        "after_bytes": total_after_bytes,
        "tally": tally,
        "errors": errors,
    }


def _print_summary(stats: dict) -> None:
    print()
    for status in sorted(stats["tally"]):
        print(f"  {status:<24} {stats['tally'][status]}")
    if stats["before_bytes"]:
        pct = 100 * (1 - stats["after_bytes"] / stats["before_bytes"])
        print(
            f"\nrows:  {stats['before_rows']:,} -> {stats['after_rows']:,}\n"
            f"bytes: {stats['before_bytes'] / 1e9:.2f} GB -> "
            f"{stats['after_bytes'] / 1e9:.2f} GB ({pct:.1f}% smaller)"
        )
    if stats["errors"]:
        print(f"\n{len(stats['errors'])} errors:", file=sys.stderr)
        for e in stats["errors"][:20]:
            print(f"  {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(str(args.config))
    if not config.s3.enabled or not config.s3.hot_bucket:
        print(
            "ERROR: s3.enabled=false or hot_bucket not set in config", file=sys.stderr
        )
        return 1

    client = _build_client(args, config)
    bucket = config.s3.hot_bucket

    if args.feed:
        # Explicit feed subset — small enough to run in-process directly
        # (spot-checks). The full run below always forks per feed instead.
        print(
            f"feeds: {len(args.feed)}  |  bucket: {bucket}  |  mode: "
            f"{'APPLY' if args.apply else 'dry run'}"
        )
        stats = _run_feeds(client, bucket, args.feed, args.apply, args.workers)
        if args.result_file:
            args.result_file.write_text(json.dumps(stats))
        _print_summary(stats)
        if not args.apply:
            print("\n(dry run — re-run with --apply to rewrite)")
        return 0

    # Full run: one fresh child process per feed. Two earlier attempts were
    # OOM-killed running thousands of partitions across all feeds in one
    # long-lived process — even after bounding peak memory for any single
    # file (compact_parquet's batching), pyarrow's memory pool still
    # accumulates unreturned memory over thousands of sequential operations.
    # A subprocess exiting fully releases everything back to the OS
    # regardless of any allocator's reluctance to return it mid-run — same
    # fix in spirit as ProcessPoolExecutor's max_tasks_per_child in
    # archiver/parallel.py, just at the per-feed granularity here. A feed
    # whose child gets OOM-killed is logged as an error and skipped rather
    # than taking down the whole backfill.
    feeds = list_feeds(client, bucket)
    if not feeds:
        print("no trip_updates feeds found", file=sys.stderr)
        return 0
    print(
        f"feeds: {len(feeds)}  |  bucket: {bucket}  |  mode: "
        f"{'APPLY' if args.apply else 'dry run'}"
    )

    grand = {
        "partitions": 0,
        "before_rows": 0,
        "after_rows": 0,
        "before_bytes": 0,
        "after_bytes": 0,
        "tally": {},
        "errors": [],
    }
    for i, feed in enumerate(feeds, 1):
        print(f"\n=== [{i}/{len(feeds)}] {feed} ===", flush=True)
        result_file = Path(tempfile.mktemp(suffix=".json"))
        cmd = [
            sys.executable,
            __file__,
            "--feed",
            feed,
            "--workers",
            str(args.workers),
            "--config",
            str(args.config),
            "--result-file",
            str(result_file),
        ]
        if args.apply:
            cmd.append("--apply")
        if args.profile:
            cmd.extend(["--profile", args.profile])
        # No capture_output: the child's own prints stream straight through
        # to this task's stdout/CloudWatch logs, same as if it ran directly.
        proc = subprocess.run(cmd)
        try:
            stats = json.loads(result_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            grand["errors"].append(
                f"{feed}: child process failed (exit {proc.returncode}), "
                "no result file — likely OOM-killed"
            )
            continue
        finally:
            result_file.unlink(missing_ok=True)
        grand["partitions"] += stats["partitions"]
        grand["before_rows"] += stats["before_rows"]
        grand["after_rows"] += stats["after_rows"]
        grand["before_bytes"] += stats["before_bytes"]
        grand["after_bytes"] += stats["after_bytes"]
        for k, v in stats["tally"].items():
            grand["tally"][k] = grand["tally"].get(k, 0) + v
        grand["errors"].extend(stats["errors"])

    print("\n\n=== GRAND TOTAL ===")
    _print_summary(grand)
    if not args.apply:
        print("\n(dry run — re-run with --apply to rewrite)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
