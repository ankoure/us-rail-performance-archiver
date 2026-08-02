import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the repo root importable when run as `python pipeline/s3_storage_metrics.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging

import boto3
from dotenv import load_dotenv

from archiver.logger import logger  # noqa: E402
from archiver.loader import build_telemetry, load_config  # noqa: E402

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Emit s3.storage.bytes gauges from CloudWatch's daily "
        "BucketSizeBytes metric, for the S3 storage-cost dashboard widgets."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _storage_types(client, bucket: str) -> list[str]:
    """StorageType dimension values CloudWatch actually publishes for this
    bucket. AWS only emits a BucketSizeBytes series per storage type present
    — there's no "AllStorageTypes" catch-all series to query directly, and
    Deep Archive splits into a base series plus per-object overhead series
    that are all separately billed, so all of them need summing."""
    resp = client.list_metrics(
        Namespace="AWS/S3",
        MetricName="BucketSizeBytes",
        Dimensions=[{"Name": "BucketName", "Value": bucket}],
    )
    return [
        dim["Value"]
        for metric in resp.get("Metrics", [])
        for dim in metric["Dimensions"]
        if dim["Name"] == "StorageType"
    ]


def _latest_bucket_size(client, bucket: str) -> float | None:
    """Return the most recent total BucketSizeBytes across all storage types
    present in the bucket, or None if the bucket has no published metrics yet.

    AWS publishes this metric once/day with no extra config (unlike Storage
    Lens or S3 request metrics), but a fresh/empty bucket may not have a
    datapoint yet — query a several-day window and take the latest per type.
    """
    storage_types = _storage_types(client, bucket)
    if not storage_types:
        return None

    now = datetime.now(timezone.utc)
    total = 0.0
    found_any = False
    for storage_type in storage_types:
        resp = client.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="BucketSizeBytes",
            Dimensions=[
                {"Name": "BucketName", "Value": bucket},
                {"Name": "StorageType", "Value": storage_type},
            ],
            StartTime=now - timedelta(days=4),
            EndTime=now,
            Period=86400,
            Statistics=["Average"],
        )
        datapoints = resp.get("Datapoints", [])
        if not datapoints:
            continue
        found_any = True
        total += max(datapoints, key=lambda d: d["Timestamp"])["Average"]
    return total if found_any else None


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    telemetry = build_telemetry(config.telemetry)
    client = boto3.client("cloudwatch", region_name=config.s3.region)

    buckets = {
        "landing": config.writer.landing_bucket,
        "cold": config.s3.cold_bucket,
        "hot": config.s3.hot_bucket,
    }
    for role, bucket in buckets.items():
        if not bucket:
            continue
        size = _latest_bucket_size(client, bucket)
        if size is None:
            logger.warning("no BucketSizeBytes datapoint yet for %s (%s)", bucket, role)
            continue
        telemetry.gauge(
            "s3.storage.bytes", size, tags={"bucket": bucket, "bucket_role": role}
        )
        logger.info("%s (%s): %.0f bytes", role, bucket, size)


if __name__ == "__main__":
    main(parse_args())
