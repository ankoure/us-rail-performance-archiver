import sys
from pathlib import Path

# Make the repo root importable when run as `python pipeline/task_duration.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging

from dotenv import load_dotenv

from archiver.logger import logger  # noqa: E402
from archiver.loader import build_telemetry, load_config  # noqa: E402

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Emit a wall-clock duration metric for the whole Fargate "
        "task run, for the ECS runtime-cost dashboard widgets. Meant to be "
        "called from a shell `trap ... EXIT` so it fires on both success and "
        "failure — a failed run is still billed for the time it ran."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    parser.add_argument(
        "--metric", default="pipeline.task.duration", help="Metric name to emit"
    )
    parser.add_argument(
        "--seconds", type=float, required=True, help="Elapsed wall-clock seconds"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    telemetry = build_telemetry(config.telemetry)
    telemetry.timing(args.metric, args.seconds * 1000)
    # build_telemetry's DogStatsd client buffers metrics and flushes on a
    # background timer (~300ms) — this script does no other work and can
    # exit before that timer ever fires, silently dropping the metric.
    # Force the flush before exit.
    client = getattr(telemetry, "client", None)
    if client is not None:
        client.flush()
    logger.info("%s: %.0fs", args.metric, args.seconds)


if __name__ == "__main__":
    main(parse_args())
