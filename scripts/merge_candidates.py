"""Append validated candidate agencies into config/feeds.yaml, idempotently.

The last stage of the onboarding pipeline (gen_feeds_from_mdb -> validate_candidates
-> merge_candidates). Appends rather than rewrites, so the existing writer/s3/telemetry
blocks and their comments stay untouched. Skips any candidate already present (by
agency_id or mdb_feed_id) so re-running after a catalog refresh only adds genuinely-new
agencies — and re-running with no changes is a no-op.

Usage:
    uv run python scripts/merge_candidates.py \\
        --candidates config/feeds.candidates.validated.yaml \\
        --target config/feeds.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.loader import load_config  # noqa: E402


def _existing_keys(target: Path) -> tuple[set[str], set[str]]:
    """(agency_ids, mdb_feed_ids) already in the target config."""
    data = yaml.safe_load(target.read_text()) or {}
    ids, mdb = set(), set()
    for a in data.get("agencies", []):
        if a.get("agency_id"):
            ids.add(a["agency_id"])
        if a.get("mdb_feed_id"):
            mdb.add(a["mdb_feed_id"])
    return ids, mdb


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--candidates",
        type=Path,
        default=Path("config/feeds.candidates.validated.yaml"),
    )
    p.add_argument("--target", type=Path, default=Path("config/feeds.yaml"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    candidates = yaml.safe_load(args.candidates.read_text()).get("agencies", [])
    have_ids, have_mdb = _existing_keys(args.target)

    new = [
        a
        for a in candidates
        if a.get("agency_id") not in have_ids and a.get("mdb_feed_id") not in have_mdb
    ]
    skipped = len(candidates) - len(new)
    if not new:
        print(
            f"[merge] nothing to add ({skipped} candidates already present)",
            file=sys.stderr,
        )
        return 0

    # Dump as a top-level sequence, then indent 2 spaces to nest under `agencies:`.
    block = yaml.safe_dump(new, sort_keys=False, allow_unicode=True, indent=2)
    indented = "".join(
        ("  " + ln if ln.strip() else ln) for ln in block.splitlines(keepends=True)
    )
    with args.target.open("a") as fh:
        fh.write(
            "\n  # ---- Onboarded from Mobility Database "
            "(scripts/gen_feeds_from_mdb.py -> validate_candidates.py) ----\n"
        )
        fh.write(indented)

    # Prove the merged file still validates through every config validator.
    cfg = load_config(str(args.target))
    print(
        f"[merge] added {len(new)} agencies ({skipped} already present) -> {args.target}; "
        f"config now {len(cfg.agencies)} agencies / "
        f"{sum(len(a.feeds) for a in cfg.agencies)} feeds",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
