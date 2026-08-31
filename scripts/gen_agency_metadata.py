"""One-time bootstrap: classify every agency in config/feeds.yaml into a
continent/region/mode-tag taxonomy for the dashboard's browse UI, writing
dashboard/api/agency_metadata.yaml.

`types` is the set of modes an agency operates -- every distinct bucket
present in the raw GTFS route_type column of its static feed (fetched via
analysis.gtfs_fetcher.GtfsResolver, using the agency's mdb_feed_id), mapped
into 5 buckets: subway_metro, light_rail_streetcar, commuter_rail, bus,
ferry_other. An agency running buses, ferries and commuter rail carries all
three tags and appears in all three sections of the browse UI.

Tags are deliberately unthresholded: a bucket earns a tag on one route. A
plurality vote (the earlier design) collapsed 144 of 201 agencies into
bus and hid every rail and ferry operation a bus-dominated agency also
runs, which is the exact thing the dashboard is for.

This intentionally does NOT reuse analysis/static_gtfs.py's route_modes /
_categorize_route_type -- that bucket collapses route_type 0 (tram/light
rail) and 1 (subway/metro) into one "rapid" mode, which is exactly the
distinction this taxonomy needs to keep. See _categorize_route_type below.

Safe to re-run: continent/region are always recomputed (cheap, deterministic
from feeds.yaml); accent_color/tagline/logo on any existing entry are always
carried forward untouched; any existing entry with types_source: manual is
left completely alone (its types are never recomputed).

Example:
    uv run python scripts/gen_agency_metadata.py
    uv run python scripts/gen_agency_metadata.py --date 2026-08-01 --output /tmp/preview.yaml
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
import yaml  # noqa: E402

from analysis.gtfs_fetcher import (  # noqa: E402
    DEFAULT_API_URL,
    DEFAULT_CACHE_DIR,
    GtfsResolver,
)
from archiver.config import AgencyConfig, ArchiverConfig  # noqa: E402
from archiver.loader import load_config  # noqa: E402
from archiver.region import continent_for  # noqa: E402

# GTFS route_type (base 0-12) and Google's extended hierarchical ranges
# (100-1799), bucketed into the 5 dashboard-facing taxonomy keys.
_BASE_TYPE_MAP: dict[int, str] = {
    0: "light_rail_streetcar",
    1: "subway_metro",
    2: "commuter_rail",
    3: "bus",
    4: "ferry_other",
    5: "ferry_other",  # cable tram (e.g. SF cable cars) — a novelty mode, not scheduled rapid transit
    6: "ferry_other",  # aerial lift
    7: "ferry_other",  # funicular
    11: "bus",  # trolleybus
    12: "subway_metro",  # monorail
}

# Stable ordering for the emitted `types` list, so re-running the generator
# produces no spurious diff and the dashboard's section order is predictable.
_TYPE_ORDER: list[str] = [
    "subway_metro",
    "light_rail_streetcar",
    "commuter_rail",
    "bus",
    "ferry_other",
]

# Agencies whose classification must never be auto-computed, seeded so the
# very first run produces a sane entry with no prior output file to carry
# forward from. On later runs, ANY existing entry marked types_source:
# manual is also preserved untouched (see build_entries) — this dict only
# matters for a from-scratch run.
_MANUAL_OVERRIDE_ENTRIES: dict[str, dict] = {
    "BAY_AREA_511": {
        "agency_id": "BAY_AREA_511",
        "continent": "us",
        "region": "San Francisco Bay Area",
        "types": ["bus"],
        "types_source": "manual",
        "types_note": (
            "Composite agency_id: SFMTA/VTA/Caltrain/ACE/Capitol Corridor/"
            "SMART share this agency_id + API key, and only SFMTA's feeds "
            "are actually polled under it today. No mdb_feed_id exists to "
            "resolve a static GTFS automatically, so the tags can't be "
            "derived — only SFMTA's buses are really archived under this "
            "id, so that is the one honest tag. Classified by hand, do "
            "not overwrite."
        ),
    },
}


def _categorize_route_type(rt: int) -> str:
    base = _BASE_TYPE_MAP.get(rt)
    if base is not None:
        return base
    if 100 <= rt <= 117 or 300 <= rt <= 307:
        return "commuter_rail"
    if 200 <= rt <= 209 or 700 <= rt <= 716 or rt == 800:
        return "bus"
    if 400 <= rt <= 405:
        return "subway_metro"
    if 900 <= rt <= 906:
        return "light_rail_streetcar"
    return "ferry_other"


def _infer_types(routes) -> list[str]:
    """Every mode bucket the agency's routes touch, in _TYPE_ORDER.

    Unthresholded on purpose: one route of a mode is enough to earn its tag,
    because a single ferry route is still a ferry service riders want to see.
    The cost is that a mis-coded route_type in an upstream feed shows the
    agency in a section it doesn't belong in; fix those by hand-editing the
    entry and setting types_source: manual to protect it.
    """
    if "route_type" not in routes.columns or routes.empty:
        return []

    counts: Counter[str] = Counter()
    for raw in routes["route_type"]:
        try:
            rt = int(raw)
        except (TypeError, ValueError):
            continue
        counts[_categorize_route_type(rt)] += 1

    known = [t for t in _TYPE_ORDER if t in counts]
    unknown = sorted(t for t in counts if t not in _TYPE_ORDER)
    return known + unknown


def _migrate_entry(entry: dict) -> dict:
    """Carry a pre-tag entry (scalar `type` + `type_confidence`) onto the tag
    schema, so a hand-locked entry survives the migration without a rewrite.
    Already-migrated entries pass through untouched."""
    if "types" in entry:
        return entry

    legacy = ("type", "type_confidence", "type_confidence_note")
    migrated = {k: v for k, v in entry.items() if k not in legacy}
    scalar = entry.get("type")
    migrated["types"] = [scalar] if scalar else []
    migrated["types_source"] = (
        "manual" if entry.get("type_confidence") == "manual" else "unresolved"
    )
    if entry.get("type_confidence_note"):
        migrated["types_note"] = entry["type_confidence_note"]
    return migrated


def build_entries(
    agencies: list[AgencyConfig],
    target_date: dt.date,
    cache_dir: Path,
    api_url: str,
    existing: dict[str, dict],
) -> list[dict]:
    entries: list[dict] = []
    resolver_cache: dict[str, GtfsResolver] = {}
    failed: set[str] = set()

    for agency in agencies:
        prior = existing.get(agency.agency_id)

        # "type_confidence" is the pre-tag spelling of types_source; still
        # honoured so a hand-locked entry written before the tag migration
        # isn't silently recomputed on the first run after it.
        if prior is not None and "manual" in (
            prior.get("types_source"),
            prior.get("type_confidence"),
        ):
            entries.append(_migrate_entry(prior))
            continue
        if prior is None and agency.agency_id in _MANUAL_OVERRIDE_ENTRIES:
            entries.append(dict(_MANUAL_OVERRIDE_ENTRIES[agency.agency_id]))
            continue

        try:
            continent = continent_for(agency.timezone)
        except ValueError as e:
            print(f"[{agency.agency_id}] {e}", file=sys.stderr)
            continent = None

        # A handful of agencies in feeds.yaml carry the literal string "nan"
        # as their region -- a pre-existing artifact of scripts/gen_feeds_from_mdb.py
        # stringifying a pandas NaN when MDB had no location data for that
        # feed. Surface it as unknown rather than a garbage region chip.
        region = agency.region if agency.region.strip().lower() != "nan" else None

        entry: dict = {
            "agency_id": agency.agency_id,
            "continent": continent,
            "region": region,
            "types": [],
            "types_source": "unresolved",
        }

        if not agency.mdb_feed_id:
            print(
                f"[{agency.agency_id}] no mdb_feed_id — left untagged",
                file=sys.stderr,
            )
        elif agency.agency_id not in failed:
            if agency.agency_id not in resolver_cache:
                resolver_cache[agency.agency_id] = GtfsResolver(
                    agency.mdb_feed_id,
                    agency.agency_id.lower(),
                    cache_dir=cache_dir,
                    api_url=api_url,
                )
            resolver = resolver_cache[agency.agency_id]
            try:
                gtfs = resolver.for_date(target_date)
            except (requests.exceptions.RequestException, LookupError) as e:
                print(
                    f"[{agency.agency_id}] GTFS unavailable ({e}) — left untagged",
                    file=sys.stderr,
                )
                failed.add(agency.agency_id)
            else:
                entry["types"] = _infer_types(gtfs.routes)
                entry["types_source"] = "gtfs" if entry["types"] else "unresolved"

        # Hand-curated polish fields survive regeneration regardless of confidence.
        if prior is not None:
            for field in ("accent_color", "tagline", "logo"):
                if prior.get(field) is not None:
                    entry[field] = prior[field]

        entries.append(entry)

    return entries


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {e["agency_id"]: e for e in raw.get("agencies", [])}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", type=Path, default=Path("config/feeds.yaml"))
    p.add_argument(
        "--output", type=Path, default=Path("dashboard/api/agency_metadata.yaml")
    )
    p.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        default=dt.date.today(),
        help="Service date to resolve each agency's static GTFS against (default: today)",
    )
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--api-url", default=DEFAULT_API_URL)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config: ArchiverConfig = load_config(str(args.config))
    existing = load_existing(args.output)

    entries = build_entries(
        config.agencies, args.date, args.cache_dir, args.api_url, existing
    )

    expected_ids = {a.agency_id for a in config.agencies}
    got_ids = {e["agency_id"] for e in entries}
    if got_ids != expected_ids:
        raise SystemExit(
            f"agency_id mismatch between {args.config} and generated entries: "
            f"{got_ids ^ expected_ids}"
        )

    header = (
        "# AUTO-GENERATED by scripts/gen_agency_metadata.py — safe to re-run.\n"
        "# `types` is a tag list: every mode an agency operates, so an agency\n"
        "# running buses, ferries and commuter rail carries all three and is\n"
        "# browsable under all three.\n"
        "# continent/region are always recomputed from config/feeds.yaml; types\n"
        "# is recomputed unless types_source is 'manual'; accent_color/tagline/\n"
        "# logo are always carried forward untouched. Hand-edit types freely --\n"
        "# set types_source: manual to protect an entry from being overwritten\n"
        "# on the next regeneration.\n\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"agencies": entries}, f, allow_unicode=True, sort_keys=False)

    counts = Counter(t for e in entries for t in e["types"])
    untagged = sum(1 for e in entries if not e["types"])
    multi = sum(1 for e in entries if len(e["types"]) > 1)
    print(f"wrote {len(entries)} agencies -> {args.output}", file=sys.stderr)
    print(f"tag distribution: {dict(counts)}", file=sys.stderr)
    print(f"{multi} agencies carry more than one mode tag", file=sys.stderr)
    print(f"{untagged} agencies need manual review (no tags resolved)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
