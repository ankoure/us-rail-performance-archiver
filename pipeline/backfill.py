from dotenv import load_dotenv
from archiver.loader import build_landing_backfill, load_config
import argparse
import logging

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill local landing window objects to S3 (soak-phase parity)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="report local-vs-S3 parity without uploading anything",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-upload even if the key already exists in S3",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="parallel upload workers (default: 8)",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    backfill = build_landing_backfill(config)
    if args.verify:
        backfill.verify()
    else:
        backfill.run(force=args.force, workers=args.workers)


if __name__ == "__main__":
    args = parse_args()
    main(args)
