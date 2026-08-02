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


def _latest_bucket_size(client, bucket: str, region: str) -> float | None:
    """Return the most recent BucketSizeBytes datapoint, or None if absent.

    AWS publishes this metric once/day with no extra config (unlike Storage
    Lens or S3 request metrics), but a fresh/empty bucket may not have a
    datapoint yet — query a 2-day window and take the latest.
    """
    now = datetime.now(timezone.utc)
    resp = client.get_metric_statistics(
        Namespace="AWS/S3",
        MetricName="BucketSizeBytes",
        Dimensions=[
            {"Name": "BucketName", "Value": bucket},
            {"Name": "StorageType", "Value": "AllStorageTypes"},
        ],
        StartTime=now - timedelta(days=2),
        EndTime=now,
        Period=86400,
        Statistics=["Average"],
    )
    datapoints = resp.get("Datapoints", [])
    if not datapoints:
        return None
    latest = max(datapoints, key=lambda d: d["Timestamp"])
    return latest["Average"]


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
        size = _latest_bucket_size(client, bucket, config.s3.region)
        if size is None:
            logger.warning("no BucketSizeBytes datapoint yet for %s (%s)", bucket, role)
            continue
        telemetry.gauge("s3.storage.bytes", size, tags={"bucket": bucket, "bucket_role": role})
        logger.info("%s (%s): %.0f bytes", role, bucket, size)


if __name__ == "__main__":
    main(parse_args())
