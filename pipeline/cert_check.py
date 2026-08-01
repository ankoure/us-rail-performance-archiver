import os
import ssl
import tempfile
import time
from urllib.parse import urlparse

from archiver.logger import logger
from dotenv import load_dotenv
from archiver.loader import build_telemetry, load_config
import argparse
import logging

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(description="Check when the ssl cert expires.")
    parser.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _cert_days_remaining(host: str) -> float:
    pem = ssl.get_server_certificate((host, 443), timeout=10)
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(pem)
        tmp = f.name
    try:
        not_after = ssl._ssl._test_decode_cert(tmp)["notAfter"]
    finally:
        os.unlink(tmp)  # tmp is guaranteed bound here
    return (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    telemetry = build_telemetry(config.telemetry)
    for agency in config.agencies:
        host = urlparse(str(agency.base_url)).hostname
        if not host:
            logger.warning("no host in base_url for %s", agency.agency_id)
            continue
        try:
            days = _cert_days_remaining(host)
        except Exception as e:
            logger.warning("cert check failed for %s: %s", agency.agency_id, e)
            continue
        telemetry.gauge(
            "cert.days_remaining",
            days,
            tags={
                "agency": agency.agency_id,
                "tls_verify": str(agency.tls_verify).lower(),
            },
        )

        logger.info("%s: %.0f days", agency.agency_id, days)


if __name__ == "__main__":
    main(parse_args())
