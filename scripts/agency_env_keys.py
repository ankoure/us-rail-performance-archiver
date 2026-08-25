"""Print the agency API-key env var names a poller box needs, one per line.

With no --continent, prints every agency's key across the whole config (what
the old, unfiltered us-east-1 box needed). With --continent, prints only the
keys agencies in that box (see archiver/region.py) actually read -- used by
each poller box's bootstrap to trim the shared Secrets Manager blob down to
just its own region's keys before writing .env, so a region-local box never
has another region's credentials sitting on disk.

Usage:
    uv run python scripts/agency_env_keys.py --continent us
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.loader import VALID_CONTINENTS, agency_env_keys, load_config  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-c", "--config", default="config/feeds.yaml")
    p.add_argument(
        "--continent",
        choices=sorted(VALID_CONTINENTS),
        default=None,
        help="Restrict to one box's agencies (default: every agency).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    for key in agency_env_keys(config, args.continent):
        print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
