"""Sync pipeline/gtfs.py's version-partitioned Parquet marts into PostGIS.

Additive geo-query layer for dashboard map features -- sits alongside the
existing Parquet marts (which keep serving dashboard/api unchanged) rather
than replacing them. Reads already-written Parquet directly off disk; never
touches a GTFS zip or the archived-feeds catalog itself, so it only depends
on `pipeline/gtfs.py` having already run for the (feed, version_slug) being
synced.

Tracks full GTFS version history: every table is keyed by
(feed, version_slug), mirroring the Parquet marts' own partitioning, so
historical snapshots coexist in Postgres rather than being overwritten by
the latest one. See pipeline/sql/postgis_schema.sql for the schema (apply
once before running this).

Idempotency mirrors gtfs.py: a (feed, version_slug) already present in
`routes` is skipped unless --force, in which case its rows across all
tables are deleted and rewritten inside one transaction.

Never touches route_shape_edits -- that table holds hand-corrected shape
geometries (e.g. edited directly in QGIS) and is intentionally decoupled
from version_slug so a correction survives every future resync. See
pipeline/sql/postgis_schema.sql's route_shapes_current view, which prefers
an edit over the synced geometry when one exists.

Examples:

    # one feed, one already-resolved GTFS version
    uv run python pipeline/postgis_sync.py --feed wmata-vehicles --version-slug 2026-05-01

    # every version already on disk for a feed
    uv run python pipeline/postgis_sync.py --feed wmata-vehicles --all-versions

    # every feed with gtfs_stops on disk, every version, re-synced
    uv run python pipeline/postgis_sync.py --all-versions --force
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
from dotenv import load_dotenv
from psycopg import sql

# Make the repo root importable when run as `python pipeline/postgis_sync.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

_GEO_MARTS = ("gtfs_stops", "gtfs_routes", "gtfs_route_aliases", "route_shapes")
_ALL_MARTS = _GEO_MARTS + ("route_shape_stops",)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--feed",
        nargs="+",
        default=None,
        help="One or more feed names. Omit to process every feed with a "
        "gtfs_stops mart on disk.",
    )
    p.add_argument(
        "--version-slug",
        help="A single GTFS version_slug to sync (requires --feed).",
    )
    p.add_argument(
        "--all-versions",
        action="store_true",
        help="Sync every version already on disk for each feed.",
    )
    p.add_argument(
        "--curated-dir",
        type=Path,
        default=Path("data/curated"),
        help="Curated root: read the gtfs_* marts from under here.",
    )
    p.add_argument(
        "--database-url",
        default=os.environ.get("POSTGIS_DATABASE_URL"),
        help="PostGIS connection string. Defaults to $POSTGIS_DATABASE_URL.",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Re-sync a (feed, version_slug) even if already present.",
    )
    args = p.parse_args(argv)
    if not args.database_url:
        p.error(
            "no PostGIS connection string: pass --database-url or set "
            "POSTGIS_DATABASE_URL"
        )
    if args.version_slug and not args.feed:
        p.error("--version-slug requires --feed")
    if not args.version_slug and not args.all_versions:
        p.error("provide --version-slug (with --feed) or --all-versions")
    return args


def discover_feeds(curated_dir: Path) -> list[str]:
    root = curated_dir / "metrics" / "gtfs_stops"
    if not root.exists():
        return []
    return sorted(
        p.name.removeprefix("feed=") for p in root.glob("feed=*") if p.is_dir()
    )


def discover_versions(curated_dir: Path, feed: str) -> list[str]:
    root = curated_dir / "metrics" / "gtfs_stops" / f"feed={feed}"
    if not root.exists():
        return []
    return sorted(
        p.name.removeprefix("version=") for p in root.glob("version=*") if p.is_dir()
    )


def discover_version_days(
    curated_dir: Path, feed: str, version_slug: str
) -> list[dict]:
    """Every gtfs_versions manifest row (across all day partitions) for this
    feed whose version_slug matches -- one row per service_date it covers."""
    root = curated_dir / "metrics" / "gtfs_versions" / f"feed={feed}"
    if not root.exists():
        return []
    rows = []
    for path in root.glob("year=*/month=*/day=*/data.parquet"):
        for row in pq.read_table(path).to_pylist():
            if row["version_slug"] == version_slug:
                rows.append(row)
    return rows


def _read_mart(
    curated_dir: Path, mart: str, feed: str, version_slug: str
) -> list[dict]:
    path = (
        curated_dir
        / "metrics"
        / mart
        / f"feed={feed}"
        / f"version={version_slug}"
        / "data.parquet"
    )
    if not path.exists():
        return []
    return pq.read_table(path).to_pylist()


def _already_synced(cur, feed: str, version_slug: str) -> bool:
    cur.execute(
        "SELECT 1 FROM routes WHERE feed = %s AND version_slug = %s LIMIT 1",
        (feed, version_slug),
    )
    return cur.fetchone() is not None


def _delete_version(cur, feed: str, version_slug: str) -> None:
    for table in (
        "route_shape_stops",
        "route_shapes",
        "route_id_crosswalk",
        "stops",
        "routes",
    ):
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE feed = %s AND version_slug = %s").format(
                sql.Identifier(table)
            ),
            (feed, version_slug),
        )


def _insert_routes(cur, feed: str, version_slug: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO routes (feed, version_slug, route_id, route_short_name, "
        "route_long_name, mode) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        [
            (
                feed,
                version_slug,
                r["route_id"],
                r["route_short_name"],
                r["route_long_name"],
                r["mode"],
            )
            for r in rows
        ],
    )
    return len(rows)


def _insert_stops(cur, feed: str, version_slug: str, rows: list[dict]) -> int:
    usable = [
        r for r in rows if r["stop_lat"] is not None and r["stop_lon"] is not None
    ]
    if not usable:
        return 0
    cur.executemany(
        "INSERT INTO stops (feed, version_slug, stop_id, stop_code, stop_name, "
        "parent_station, geom) VALUES (%s, %s, %s, %s, %s, %s, "
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)) ON CONFLICT DO NOTHING",
        [
            (
                feed,
                version_slug,
                r["stop_id"],
                r["stop_code"],
                r["stop_name"],
                r["parent_station"],
                r["stop_lon"],
                r["stop_lat"],
            )
            for r in usable
        ],
    )
    return len(usable)


def _insert_crosswalk(cur, feed: str, version_slug: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    # Row order mirrors routes.txt file order (see gtfs.py's
    # _route_aliases_rows), so ON CONFLICT DO NOTHING reproduces the same
    # keep-first precedence as StaticGtfs._keepfirst_index.
    cur.executemany(
        "INSERT INTO route_id_crosswalk (feed, version_slug, alias_token, "
        "alias_type, route_id) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        [
            (feed, version_slug, r["alias_token"], r["alias_type"], r["route_id"])
            for r in rows
        ],
    )
    return len(rows)


def _insert_route_shapes(cur, feed: str, version_slug: str, rows: list[dict]) -> int:
    by_shape: dict[tuple[str, int, str], list[dict]] = {}
    for r in rows:
        key = (r["route_id"], r["direction_id"], r["shape_id"])
        by_shape.setdefault(key, []).append(r)
    if not by_shape:
        return 0
    values = []
    for (route_id, direction_id, shape_id), points in by_shape.items():
        points.sort(key=lambda r: r["point_sequence"])
        if len(points) < 2:
            continue
        wkt = "LINESTRING(" + ", ".join(f"{p['lon']} {p['lat']}" for p in points) + ")"
        length_m = points[-1]["dist_m"]
        values.append(
            (feed, version_slug, route_id, direction_id, shape_id, wkt, length_m)
        )
    if not values:
        return 0
    cur.executemany(
        "INSERT INTO route_shapes (feed, version_slug, route_id, direction_id, "
        "shape_id, geom, length_m) VALUES (%s, %s, %s, %s, %s, "
        "ST_GeomFromText(%s, 4326), %s) ON CONFLICT DO NOTHING",
        [
            (f, v, rid, d, sid, wkt, length_m)
            for f, v, rid, d, sid, wkt, length_m in values
        ],
    )
    return len(values)


def _insert_route_shape_stops(
    cur, feed: str, version_slug: str, rows: list[dict]
) -> int:
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO route_shape_stops (feed, version_slug, route_id, "
        "direction_id, shape_id, stop_id, dist_m) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        [
            (
                feed,
                version_slug,
                r["route_id"],
                r["direction_id"],
                r["shape_id"],
                r["stop_id"],
                r["dist_m"],
            )
            for r in rows
        ],
    )
    return len(rows)


def _upsert_versions(cur, feed: str, version_rows: list[dict]) -> int:
    if not version_rows:
        return 0
    cur.executemany(
        "INSERT INTO gtfs_versions (feed, service_date, version_slug, "
        "feed_version, feed_start_date, feed_end_date) VALUES "
        "(%s, %s, %s, %s, %s, %s) ON CONFLICT (feed, service_date) DO UPDATE "
        "SET version_slug = EXCLUDED.version_slug, "
        "feed_version = EXCLUDED.feed_version, "
        "feed_start_date = EXCLUDED.feed_start_date, "
        "feed_end_date = EXCLUDED.feed_end_date",
        [
            (
                feed,
                r["service_date"],
                r["version_slug"],
                r["feed_version"],
                r["feed_start_date"],
                r["feed_end_date"],
            )
            for r in version_rows
        ],
    )
    return len(version_rows)


def sync_feed_version(
    conn, curated_dir: Path, feed: str, version_slug: str, force: bool
) -> dict | None:
    """Sync one (feed, version_slug) into PostGIS. Returns a counts dict, or
    None if already synced and not --force."""
    with conn.cursor() as cur:
        if not force and _already_synced(cur, feed, version_slug):
            return None

        mart_rows = {
            mart: _read_mart(curated_dir, mart, feed, version_slug)
            for mart in _ALL_MARTS
        }
        version_rows = discover_version_days(curated_dir, feed, version_slug)

        _delete_version(cur, feed, version_slug)
        counts = {
            "routes": _insert_routes(cur, feed, version_slug, mart_rows["gtfs_routes"]),
            "stops": _insert_stops(cur, feed, version_slug, mart_rows["gtfs_stops"]),
            "route_id_crosswalk": _insert_crosswalk(
                cur, feed, version_slug, mart_rows["gtfs_route_aliases"]
            ),
            "route_shapes": _insert_route_shapes(
                cur, feed, version_slug, mart_rows["route_shapes"]
            ),
            "route_shape_stops": _insert_route_shape_stops(
                cur, feed, version_slug, mart_rows["route_shape_stops"]
            ),
            "gtfs_versions": _upsert_versions(cur, feed, version_rows),
        }
    conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    feeds = args.feed if args.feed else discover_feeds(args.curated_dir)
    if not feeds:
        print("no feeds found under curated_dir", file=sys.stderr)
        return 1

    totals = {
        k: 0
        for k in (
            "routes",
            "stops",
            "route_id_crosswalk",
            "route_shapes",
            "route_shape_stops",
            "gtfs_versions",
        )
    }
    synced, skipped = 0, 0

    with psycopg.connect(args.database_url) as conn:
        for feed in feeds:
            versions = (
                [args.version_slug]
                if args.version_slug
                else discover_versions(args.curated_dir, feed)
            )
            if not versions:
                print(f"[{feed}] no versions on disk — skipping", file=sys.stderr)
                continue
            for version_slug in versions:
                result = sync_feed_version(
                    conn, args.curated_dir, feed, version_slug, args.force
                )
                if result is None:
                    print(f"[{feed}] version {version_slug}: already synced, skipping")
                    skipped += 1
                    continue
                synced += 1
                for k in totals:
                    totals[k] += result[k]
                print(
                    f"[{feed}] version {version_slug}: "
                    f"{result['routes']:,} routes  "
                    f"{result['stops']:,} stops  "
                    f"{result['route_id_crosswalk']:,} crosswalk rows  "
                    f"{result['route_shapes']:,} route_shapes  "
                    f"{result['route_shape_stops']:,} route_shape_stops  "
                    f"{result['gtfs_versions']:,} gtfs_versions rows"
                )

    print(f"---\nsynced {synced} version(s), skipped {skipped} already-synced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
