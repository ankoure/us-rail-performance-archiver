"""Diff a feed's stops.txt across GTFS static snapshots to check stop_id stability.

Built for Phase 4 of the speed-dashboard initiative (see
docs/design/stop-id-stability-findings.md) and kept here as a reusable
diagnostic for checking any other agency the same way. Not part of the
pipeline -- a one-off/on-demand research tool, run manually.

Default mode diffs just the two endpoint snapshots (early/late), which is
cheap but structurally blind to flapping: a stop_id that's removed and
re-added, or renamed and reverted, between the two dates looks perfectly
stable. --walk instead loads every distinct snapshot in the catalog between
the two dates and tracks each stop_id's presence/name/coordinates across all
of them, so it can catch that kind of round-trip churn. It's a much heavier
scan (one download+parse per catalog version -- MBTA alone has ~190), so use
it when flapping specifically is the question, not as the default.

Reports, for the stop_id set common to both snapshots, how many were renamed
(same id, different stop_name) and how many moved (>--move-threshold-m).
Both together is the strongest available signal for genuine id recycling to
a different physical location -- watch for a misleadingly high "renamed"
count that's actually one bulk naming-convention change across the whole
feed (see the WMATA ALL-CAPS-to-Title-Case case in the findings doc): always
check the renamed-AND-moved intersection, and sample a few examples, before
trusting the raw renamed count.

Examples:

    # defaults to the catalog's earliest and latest available snapshots
    uv run python scripts/stop_id_stability.py --feed-id mdb-437 --agency mbta

    # explicit dates, more samples, and isolate ids under a parent_station
    # naming convention (MBTA's rapid-transit stations all start "place-")
    uv run python scripts/stop_id_stability.py --feed-id mdb-437 --agency mbta \\
        --early-date 2024-05-01 --late-date 2026-07-30 \\
        --parent-prefix place- --samples 15

    # walk every intermediate snapshot to catch flapping the endpoint diff can't see
    uv run python scripts/stop_id_stability.py --feed-id mdb-437 --agency mbta --walk
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.gtfs_fetcher import (  # noqa: E402
    DEFAULT_API_URL,
    DEFAULT_CACHE_DIR,
    Snapshot,
    ensure_local_zip,
    fetch_catalog,
    pick_snapshot,
)
from analysis.static_gtfs import StaticGtfs  # noqa: E402

# ~0.0005 degrees is roughly 50m at mid-latitudes -- close enough for "did
# this stop actually move" without being sensitive to coordinate-precision
# noise (e.g. a rough station-centroid coordinate later replaced by a
# surveyed per-platform one).
DEFAULT_MOVE_THRESHOLD_DEG = 0.0005


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed-id", required=True, help="MobilityDatabase feed ID, e.g. mdb-437"
    )
    p.add_argument(
        "--agency", required=True, help="Short agency slug for cache path, e.g. mbta"
    )
    p.add_argument(
        "--early-date",
        type=dt.date.fromisoformat,
        help="Earlier service date YYYY-MM-DD (default: catalog's earliest snapshot)",
    )
    p.add_argument(
        "--late-date",
        type=dt.date.fromisoformat,
        help="Later service date YYYY-MM-DD (default: catalog's latest snapshot)",
    )
    p.add_argument(
        "--move-threshold-deg",
        type=float,
        default=DEFAULT_MOVE_THRESHOLD_DEG,
        help=f'Lat/lon delta counted as "moved" (default: {DEFAULT_MOVE_THRESHOLD_DEG}, ~50m)',
    )
    p.add_argument(
        "--parent-prefix",
        help="If set, also report separately on stop_ids whose parent_station "
        "starts with this prefix (e.g. MBTA rapid-transit stations: place-), "
        "and on top-level ids starting with the same prefix (the stations "
        "themselves).",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=10,
        help="How many example renamed/moved/flapping rows to print (default: 10)",
    )
    p.add_argument(
        "--walk",
        action="store_true",
        help="Also load every distinct catalog snapshot between --early-date and "
        "--late-date (not just the two endpoints) and check for flapping: ids "
        "that disappear and reappear, or rename/move and revert, along the way. "
        "One download+parse per snapshot -- heavier than the default endpoint diff.",
    )
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--api-url", default=DEFAULT_API_URL)
    return p.parse_args(argv)


def load_snapshot(
    feed_id: str, agency_slug: str, target_date: dt.date, cache_dir: Path, api_url: str
):
    catalog = fetch_catalog(feed_id, api_url=api_url)
    snap = pick_snapshot(catalog, target_date)
    path = ensure_local_zip(snap, agency_slug, cache_dir=cache_dir)
    return StaticGtfs(path), snap, catalog


def stops_frame(gtfs: StaticGtfs) -> pd.DataFrame:
    """De-duplicated stops.txt indexed by stop_id."""
    df = gtfs.stops.dropna(subset=["stop_id"])
    return df.drop_duplicates(subset=["stop_id"], keep="first").set_index("stop_id")


def diff_ids(
    df_early: pd.DataFrame, df_late: pd.DataFrame, move_threshold_deg: float
) -> dict:
    ids_early, ids_late = set(df_early.index), set(df_late.index)
    common = ids_early & ids_late

    renamed, moved, both = [], [], []
    for sid in common:
        a, b = df_early.loc[sid], df_late.loc[sid]
        name_a, name_b = a.get("stop_name"), b.get("stop_name")
        is_renamed = (
            isinstance(name_a, str) and isinstance(name_b, str) and name_a != name_b
        )
        is_moved = False
        try:
            lat_a, lon_a = float(a.get("stop_lat")), float(a.get("stop_lon"))
            lat_b, lon_b = float(b.get("stop_lat")), float(b.get("stop_lon"))
            is_moved = (
                abs(lat_a - lat_b) > move_threshold_deg
                or abs(lon_a - lon_b) > move_threshold_deg
            )
        except (TypeError, ValueError):
            pass
        if is_renamed:
            renamed.append((sid, name_a, name_b))
        if is_moved:
            moved.append((sid, name_a, name_b))
        if is_renamed and is_moved:
            both.append((sid, name_a, name_b))

    return {
        "ids_early": ids_early,
        "ids_late": ids_late,
        "common": common,
        "added": ids_late - ids_early,
        "removed": ids_early - ids_late,
        "renamed": renamed,
        "moved": moved,
        "renamed_and_moved": both,
    }


def print_report(label: str, result: dict, samples: int) -> None:
    n_early = len(result["ids_early"])
    n_common = len(result["common"])
    pct = 100 * n_common / n_early if n_early else 0.0
    print(f"\n{label}")
    print(f"  count: {n_early} -> {len(result['ids_late'])}")
    print(f"  stable: {n_common} ({pct:.1f}% of early set)")
    print(f"  added: {len(result['added'])}  removed: {len(result['removed'])}")
    print(
        f"  of stable ids: renamed={len(result['renamed'])}, "
        f"moved={len(result['moved'])}, renamed-AND-moved (recycling signal)="
        f"{len(result['renamed_and_moved'])}"
    )
    if result["renamed_and_moved"]:
        print(f"  renamed-AND-moved examples (up to {samples}):")
        for sid, a, b in result["renamed_and_moved"][:samples]:
            print(f"    {sid}: {a!r} -> {b!r}")
    elif result["renamed"]:
        print(
            f"  renamed-only examples (up to {samples}) -- sample before trusting this count,"
        )
        print(
            "  a large renamed-only total with few/no renamed-AND-moved cases usually means a"
        )
        print("  bulk naming-convention change, not per-stop identity churn:")
        for sid, a, b in result["renamed"][:samples]:
            print(f"    {sid}: {a!r} -> {b!r}")


def all_snapshots_in_range(
    catalog: pd.DataFrame, early: dt.date, late: dt.date
) -> list[Snapshot]:
    """Every distinct snapshot (by version_slug) whose feed_start_date falls in
    [early, late], sorted chronologically. Unlike pick_snapshot (which resolves
    one target date to the single snapshot in effect that day), this returns
    every version the catalog knows about across the whole range -- the set
    --walk needs to actually see stop_ids come and go between the endpoints."""
    mask = (catalog["feed_start_date"] >= pd.Timestamp(early)) & (
        catalog["feed_start_date"] <= pd.Timestamp(late)
    )
    rows = catalog[mask].sort_values("feed_start_date")
    snapshots: list[Snapshot] = []
    seen_versions: set[str] = set()
    for _, row in rows.iterrows():
        snap = Snapshot(
            feed_start_date=row["feed_start_date"].date(),
            feed_end_date=row["feed_end_date"].date(),
            feed_version=str(row["feed_version"]),
            archive_url=str(row["archive_url"]),
        )
        if snap.version_slug in seen_versions:
            continue
        seen_versions.add(snap.version_slug)
        snapshots.append(snap)
    return snapshots


def walk_snapshots(
    feed_id: str, agency_slug: str, snapshots: list[Snapshot], cache_dir: Path
) -> list[tuple[Snapshot, pd.DataFrame]]:
    """Download+parse every given snapshot's stops.txt. Skips (with a stderr
    warning) any snapshot whose zip fails to fetch -- a few dead archive_urls
    among ~190 versions shouldn't abort the whole walk."""
    walked: list[tuple[Snapshot, pd.DataFrame]] = []
    for i, snap in enumerate(snapshots, 1):
        print(
            f"[{i}/{len(snapshots)}] loading {feed_id} {snap.version_slug} "
            f"({snap.feed_start_date})...",
            file=sys.stderr,
        )
        try:
            path = ensure_local_zip(snap, agency_slug, cache_dir=cache_dir)
            gtfs = StaticGtfs(path)
            walked.append((snap, stops_frame(gtfs)))
        except (requests.exceptions.RequestException, OSError) as e:
            print(f"    SKIP -- {e}", file=sys.stderr)
    return walked


def detect_flapping(
    walked: list[tuple[Snapshot, pd.DataFrame]], move_threshold_deg: float
) -> dict:
    """Find stop_ids whose presence, name, or coordinates go back and forth
    across the walked snapshots -- churn an endpoint-only diff can't see.

    flapping: disappeared and later reappeared (>=1 full gap-and-return).
    reverted_names / reverted_coords: took on a value, changed away from it,
    then returned to a previously-seen value -- a one-time permanent rename
    (A -> B) is NOT flagged, only a genuine round-trip (A -> B -> A).
    """
    all_ids: set[str] = set()
    for _, df in walked:
        all_ids |= set(df.index)

    flapping: list[tuple[str, list[tuple[str, str]]]] = []
    reverted_names: list[tuple[str, list[str]]] = []
    reverted_coords: list[tuple[str, list[tuple[float, float]]]] = []

    for sid in all_ids:
        presence: list[tuple[str, bool]] = []
        name_seq: list[str] = []
        coord_seq: list[tuple[float, float]] = []
        for snap, df in walked:
            present = sid in df.index
            presence.append((snap.version_slug, present))
            if not present:
                continue
            row = df.loc[sid]
            name = row.get("stop_name")
            if isinstance(name, str) and (not name_seq or name_seq[-1] != name):
                name_seq.append(name)
            try:
                coord = (
                    round(float(row.get("stop_lat")), 4),
                    round(float(row.get("stop_lon")), 4),
                )
                if not coord_seq or (
                    abs(coord_seq[-1][0] - coord[0]) > move_threshold_deg
                    or abs(coord_seq[-1][1] - coord[1]) > move_threshold_deg
                ):
                    coord_seq.append(coord)
            except (TypeError, ValueError):
                pass

        runs = 0
        prev = False
        gap_events: list[tuple[str, str]] = []
        for vslug, present in presence:
            if present and not prev:
                runs += 1
                if runs > 1:
                    gap_events.append(("re-added", vslug))
            elif not present and prev:
                gap_events.append(("removed", vslug))
            prev = present
        if runs >= 2:
            flapping.append((sid, gap_events))

        if len(name_seq) >= 3 and len(set(name_seq)) < len(name_seq):
            reverted_names.append((sid, name_seq))
        if len(coord_seq) >= 3 and len(set(coord_seq)) < len(coord_seq):
            reverted_coords.append((sid, coord_seq))

    return {
        "n_snapshots": len(walked),
        "n_ids_seen": len(all_ids),
        "flapping": flapping,
        "reverted_names": reverted_names,
        "reverted_coords": reverted_coords,
    }


def print_flapping_report(label: str, result: dict, samples: int) -> None:
    print(
        f"\n{label} (walked {result['n_snapshots']} snapshots, {result['n_ids_seen']} distinct ids seen)"
    )
    print(
        f"  flapping (removed then re-added): {len(result['flapping'])}  "
        f"reverted names (A->B->A): {len(result['reverted_names'])}  "
        f"reverted coords: {len(result['reverted_coords'])}"
    )
    if result["flapping"]:
        print(f"  flapping examples (up to {samples}):")
        for sid, events in result["flapping"][:samples]:
            events_str = ", ".join(f"{kind}@{vslug}" for kind, vslug in events)
            print(f"    {sid}: {events_str}")
    if result["reverted_names"]:
        print(f"  reverted-name examples (up to {samples}):")
        for sid, names in result["reverted_names"][:samples]:
            print(f"    {sid}: {' -> '.join(names)}")
    if result["reverted_coords"]:
        print(f"  reverted-coord examples (up to {samples}):")
        for sid, coords in result["reverted_coords"][:samples]:
            print(f"    {sid}: {' -> '.join(str(c) for c in coords)}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog = fetch_catalog(args.feed_id, api_url=args.api_url)
    early_date = (
        args.early_date
        or pick_snapshot(
            catalog, catalog["feed_start_date"].min().date()
        ).feed_start_date
    )
    late_date = (
        args.late_date
        or pick_snapshot(
            catalog, catalog["feed_start_date"].max().date()
        ).feed_start_date
    )

    gtfs_early, snap_early, _ = load_snapshot(
        args.feed_id, args.agency, early_date, args.cache_dir, args.api_url
    )
    gtfs_late, snap_late, _ = load_snapshot(
        args.feed_id, args.agency, late_date, args.cache_dir, args.api_url
    )
    print(
        f"{args.agency} ({args.feed_id}): {early_date} ({snap_early.version_slug}) "
        f"-> {late_date} ({snap_late.version_slug})"
    )

    df_early, df_late = stops_frame(gtfs_early), stops_frame(gtfs_late)
    print_report(
        "All stop_ids",
        diff_ids(df_early, df_late, args.move_threshold_deg),
        args.samples,
    )

    if args.parent_prefix:
        prefix = args.parent_prefix
        platforms_early = df_early[
            df_early["parent_station"].fillna("").str.startswith(prefix)
        ]
        platforms_late = df_late[
            df_late["parent_station"].fillna("").str.startswith(prefix)
        ]
        print_report(
            f"Platform-level ids (parent_station starts with {prefix!r})",
            diff_ids(platforms_early, platforms_late, args.move_threshold_deg),
            args.samples,
        )

        stations_early = df_early[df_early.index.str.startswith(prefix)]
        stations_late = df_late[df_late.index.str.startswith(prefix)]
        result = diff_ids(stations_early, stations_late, args.move_threshold_deg)
        print(f"\nStation-level ids (stop_id starts with {prefix!r})")
        print(f"  count: {len(result['ids_early'])} -> {len(result['ids_late'])}")
        print(
            f"  stable: {len(result['common'])}  added: {sorted(result['added'])}  "
            f"removed: {sorted(result['removed'])}"
        )

    if args.walk:
        snapshots = all_snapshots_in_range(catalog, early_date, late_date)
        print(
            f"\n--walk: {len(snapshots)} distinct snapshots between {early_date} and {late_date}",
            file=sys.stderr,
        )
        walked = walk_snapshots(args.feed_id, args.agency, snapshots, args.cache_dir)

        print_flapping_report(
            "All stop_ids",
            detect_flapping(walked, args.move_threshold_deg),
            args.samples,
        )

        if args.parent_prefix:
            prefix = args.parent_prefix
            walked_platforms = [
                (snap, df[df["parent_station"].fillna("").str.startswith(prefix)])
                for snap, df in walked
            ]
            print_flapping_report(
                f"Platform-level ids (parent_station starts with {prefix!r})",
                detect_flapping(walked_platforms, args.move_threshold_deg),
                args.samples,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
