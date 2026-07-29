import argparse
import base64
import json
from pathlib import Path
import sys

import rail_decoder

# rail_decoder's DecodedRows uses snake_case attribute names, but the golden
# fixture files are named after the Python Row dataclasses. This bridges the two.
ROW_TYPES = {
    "VehicleRow": "vehicles",
    "StopTimeUpdateRow": "stop_time_updates",
    "AlertRow": "alerts",
}


def load_payloads(fixture_dir: Path) -> list[dict]:
    """Load payloads.json, same shape gen_golden_files.py wrote it in."""
    with open(fixture_dir / "payloads.json", "r", encoding="utf-8") as file:
        data = json.load(file)

    return data


def decode_all(payloads: list[dict]) -> dict[str, list]:
    """Run every payload's bytes through rail_decoder.decode, concatenating
    .vehicles / .stop_time_updates / .alerts across all payloads in order.
    Returns {"vehicles": [...], "stop_time_updates": [...], "alerts": [...]}.
    """
    vehicles, stop_time_updates, alerts = [], [], []
    for payload in payloads:
        payload_bytes = base64.b64decode(payload["payload"])
        decoded = rail_decoder.decode(payload_bytes)
        vehicles.extend(decoded.vehicles)
        stop_time_updates.extend(decoded.stop_time_updates)
        alerts.extend(decoded.alerts)
    return {
        "vehicles": vehicles,
        "stop_time_updates": stop_time_updates,
        "alerts": alerts,
    }


def row_to_dict(row) -> dict:
    """Convert a rail_decoder pyclass row instance into a plain dict, using
    its #[pyo3(get)] attributes."""
    fields = [a for a in dir(row) if not a.startswith("_")]
    return {attr: getattr(row, attr) for attr in fields}


def diff_rows(actual: list[dict], expected: list[dict]) -> list[str]:
    """Compare two lists of row-dicts. Returns a list of human-readable
    mismatch descriptions; empty list means a perfect match."""
    mismatches = []
    if len(actual) != len(expected):
        mismatches.append(
            f"Row count mismatch: actual {len(actual)} vs expected {len(expected)}"
        )

    for i, (a, e) in enumerate(zip(actual, expected)):
        for key in e.keys():
            if a.get(key) != e[key]:
                mismatches.append(
                    f"Row {i} key '{key}' mismatch: actual {a.get(key)} vs expected {e[key]}"
                )

    return mismatches


def check_feed(fixture_dir: Path) -> bool:
    """Run the full check for one feed's fixture directory. Returns True if
    everything matched, False if any mismatches were found (and prints them)."""
    payloads = load_payloads(fixture_dir)
    decoded = decode_all(payloads)

    all_ok = True
    for golden_name, rust_attr in ROW_TYPES.items():
        golden_path = fixture_dir / f"{golden_name}.json"
        if not golden_path.exists():
            continue  # this feed doesn't produce this row type (e.g. nyct-l has no alerts)

        expected = json.loads(golden_path.read_text())
        actual = [row_to_dict(r) for r in decoded[rust_attr]]

        mismatches = diff_rows(actual, expected)
        if mismatches:
            all_ok = False
            print(f"Mismatches found in {golden_name}:")
            for mismatch in mismatches:
                print(f"  {mismatch}")

    return all_ok


def main(args):
    success = check_feed(args.fixture_dir)
    if not success:
        sys.exit(1)
    else:
        print("All rows matched golden fixtures.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check that rail_decoder output matches golden fixtures."
    )
    parser.add_argument(
        "fixture_dir",
        type=Path,
        help="Directory containing payloads.json and golden row files.",
    )
    args = parser.parse_args()
    main(args)
