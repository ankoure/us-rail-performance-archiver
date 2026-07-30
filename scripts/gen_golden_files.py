import argparse
from collections import defaultdict
import json
import logging
from pathlib import Path
from datetime import date
import sys
from dotenv import load_dotenv
from dataclasses import asdict
import base64

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from archiver.decoder import StandardDecoder  # noqa: E402
from archiver.loader import build_rollup, load_config, build_source  # noqa: E402
from archiver.parser import ProtobufParser  # noqa: E402
from archiver.logger import setup_logging  # noqa: E402

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(description="Archive configured feeds")
    parser.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    parser.add_argument(
        "-o",
        "--output_folder",
        default="tests/fixtures/golden",
        help="Folder path for output file",
    )
    parser.add_argument(
        "-f",
        "--feed_name",
        default="nyct-l",
        help="Feed name to process",
    )
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        help="Specify day to process (YYYY-MM-DD)",
        default=date(2026, 5, 30),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Specify how many payloads to process",
        default=30,
    )

    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def collect_payloads(source, rollup, feed_name, day, digest_ts, limit):
    payloads = []
    for name, blob in source.iter_bins(feed_name, day):
        for payload_bytes, fetched_at in rollup._iter_payloads(
            name=name, data=blob, digest_ts=digest_ts
        ):
            encoded = base64.b64encode(payload_bytes)  # -> bytes, e.g. b'CAY6...
            encoded_str = encoded.decode("ascii")  # -> str, now JSON-safe
            # {"payload": ..., "fetched_at": ...}
            payload_dict = {"payload": encoded_str, "fetched_at": fetched_at}
            payloads.append(payload_dict)
            if len(payloads) >= limit:
                return payloads
    return payloads


def main(args):
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    source = build_source(config)
    rollup = build_rollup(config)
    rows_by_type = defaultdict(list)
    digest_ts = rollup._digest_timestamps(feed_name=args.feed_name, day=args.day)
    payloads = collect_payloads(
        source, rollup, args.feed_name, args.day, digest_ts, args.limit
    )
    for payload in payloads:
        payload_bytes = base64.b64decode(payload["payload"])
        fetched_at = payload["fetched_at"]
        parsed = ProtobufParser().parse(payload_bytes)
        for row in StandardDecoder().decode(parsed, fetched_at=fetched_at):
            type_name = type(row).__name__
            rows_by_type[type_name].append(asdict(row))

    for type_name, rows in rows_by_type.items():
        out_path = Path(args.output_folder) / f"{type_name}.json"
        with open(out_path, "w") as json_file:
            json.dump(rows, json_file)

    out_path = Path(args.output_folder) / "payloads.json"
    with open(out_path, "w") as json_file:
        json.dump(payloads, json_file)


if __name__ == "__main__":
    args = parse_args()
    main(args)
