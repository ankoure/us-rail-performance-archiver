"""Pull 511.org's monthly historic GTFS-RT archive and land it in S3, raw.

511.org's `datafeeds` endpoint serves a monthly zip per operator; appending
`-so` to the `historic` parameter adds `stop_observations.txt` (observed
real-time arrivals per stop per trip) on top of that month's static GTFS:

    https://api.511.org/transit/datafeeds?api_key=...&operator_id=RG&historic=YYYY-MM-so

`RG` (regional) bundles all six Bay Area rail operators this project already
polls live under the single `BAY_AREA_511` agency in config/feeds.yaml
(SFMTA, VTA, Caltrain, ACE, Capitol Corridor, SMART) into one combined
archive. Reuses that agency's base_url and API-key env var rather than
hardcoding either.

This just lands the raw zip in the hot bucket under historic/511/<operator>/ —
parsing it into the standard trip_updates/vehicles schema is a later step.
Meant to run as the rail-archiver-historic-511 ECS task
(terraform/historic_511.tf), monthly; runs the same locally.

Example:
    uv run python pipeline/historic_511.py --month 2026-06
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import tempfile
from pathlib import Path

# Make the repo root importable when run as `python pipeline/historic_511.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from archiver.config import AgencyConfig
from archiver.loader import build_telemetry, build_uploader, load_config  # noqa: E402
from archiver.logger import logger  # noqa: E402

load_dotenv()

_AGENCY_ID = "BAY_AREA_511"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--operator",
        default="RG",
        help="511 operator_id to pull (default: RG, regional).",
    )
    p.add_argument(
        "--month",
        default=None,
        help="Historic month to pull (YYYY-MM). Defaults to the previous UTC month.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="re-download/re-upload even if this month's archive is already shipped",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _previous_month(today: dt.date) -> str:
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - dt.timedelta(days=1)
    return last_month_end.strftime("%Y-%m")


def _find_agency(agencies: list[AgencyConfig], agency_id: str) -> AgencyConfig:
    for agency in agencies:
        if agency.agency_id == agency_id:
            return agency
    raise RuntimeError(f"agency {agency_id!r} not found in config")


def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    month = args.month or _previous_month(dt.datetime.now(dt.timezone.utc).date())

    agency = _find_agency(config.agencies, _AGENCY_ID)
    if agency.auth.type != "api_key" or not agency.auth.param:
        raise RuntimeError(f"{_AGENCY_ID} agency auth must be a query-param api_key")
    api_key = os.environ[agency.auth.env]

    telemetry = build_telemetry(config.telemetry)
    uploader = build_uploader(config.s3, telemetry)
    hot_bucket = config.s3.hot_bucket

    key = f"{config.s3.hot_prefix}historic/511/{args.operator.lower()}/{month}-so.zip"
    if not args.force and uploader.exists(hot_bucket, key):
        logger.info("[%s %s] already shipped — skipping", args.operator, month)
        return 0

    url = f"{str(agency.base_url).rstrip('/')}/datafeeds"
    params = {
        agency.auth.param: api_key,
        "operator_id": args.operator,
        "historic": f"{month}-so",
    }

    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        tmp_path = Path(tmp.name)
        with requests.get(url, params=params, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "zip" not in content_type:
                raise RuntimeError(
                    f"expected a zip archive, got Content-Type {content_type!r}"
                )
            size = 0
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                tmp.write(chunk)
                size += len(chunk)
        tmp.flush()

        logger.info(
            "[%s %s] downloaded %d bytes — uploading to s3://%s/%s",
            args.operator,
            month,
            size,
            hot_bucket,
            key,
        )
        uploader.upload(hot_bucket, key, tmp_path)

    telemetry.gauge(
        "historic.511.bytes",
        size,
        tags={"operator": args.operator, "month": month},
    )
    logger.info(
        "[%s %s] shipped %d bytes to s3://%s/%s",
        args.operator,
        month,
        size,
        hot_bucket,
        key,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
