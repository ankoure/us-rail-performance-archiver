"""Migrate curated GTFS-rt parquet to the canonical (DOTTED) column schema.

The ``trip_updates`` and ``vehicles`` curated datasets carry three different
column-naming conventions depending on when they were written (see
docs/design/rt-schema-drift.md):

    FLAT    (through 2026-05-16)  -> ``arrival_delay``, ``vehicle_id`` ...
    HYBRID  (2026-05-19..05-25)   -> a half-applied mix of flat + dotted names
    DOTTED  (2026-05-30 onward)   -> ``trip_update.stop_time_update.arrival.delay`` ...

DOTTED is the current majority convention, so it is the migration target. This
script rewrites every FLAT/HYBRID file to the DOTTED schema:

  * flat names are renamed to their dotted protobuf path (one map covers both
    FLAT and HYBRID, since HYBRID just leaves some of the same flat names
    un-renamed);
  * columns the flat era never wrote are added as typed nulls (the data is gone,
    but the column exists so a whole-dataset read unifies);
  * columns are reordered/cast to the canonical schema.

Already-DOTTED files match the target and are skipped (the migration is
idempotent). Type checks across eras confirmed no type drift hides under the
renames, so this is a pure naming + null-padding pass.

Writes are atomic (temp file + os.replace) and in place. Defaults to a dry run;
pass --apply to actually rewrite.

Examples:

    # preview what would change across the whole curated tree
    uv run python scripts/migrate_rt_schema.py

    # migrate both datasets in place
    uv run python scripts/migrate_rt_schema.py --apply

    # just one dataset, more workers
    uv run python scripts/migrate_rt_schema.py --apply --dataset vehicles --workers 16
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# --------------------------------------------------------------------------- #
# Canonical (DOTTED) schemas — the migration target. Field order here is the
# order written out. Types were verified identical across all three eras for
# every shared column, so casts are no-ops; the only typed-from-scratch columns
# are the flat-era-missing ones (null-padded).
# --------------------------------------------------------------------------- #

_LABEL_LIST = pa.list_(pa.struct([pa.field("label", pa.string())]))

TRIP_UPDATES_SCHEMA = pa.schema(
    [
        pa.field("feed_timestamp", pa.int64()),
        pa.field("trip_update.timestamp", pa.int64()),  # flat-era: missing
        pa.field("trip_update.trip.trip_id", pa.string()),
        pa.field("trip_update.trip.route_id", pa.string()),
        pa.field("trip_update.trip.direction_id", pa.int64()),
        pa.field("trip_update.trip.start_date", pa.string()),
        pa.field("trip_update.trip.start_time", pa.string()),
        pa.field("trip_update.trip.schedule_relationship", pa.string()),
        pa.field("trip_update.vehicle.id", pa.string()),
        pa.field("trip_update.vehicle.label", pa.string()),
        pa.field("trip_update.stop_time_update.stop_sequence", pa.int64()),
        pa.field("trip_update.stop_time_update.stop_id", pa.string()),
        pa.field("trip_update.stop_time_update.arrival.delay", pa.int64()),
        pa.field("trip_update.stop_time_update.arrival.time", pa.int64()),
        pa.field("trip_update.stop_time_update.arrival.uncertainty", pa.int64()),
        pa.field("trip_update.stop_time_update.departure.delay", pa.int64()),
        pa.field("trip_update.stop_time_update.departure.time", pa.int64()),
        pa.field("trip_update.stop_time_update.departure.uncertainty", pa.int64()),
        pa.field("trip_update.stop_time_update.schedule_relationship", pa.string()),
    ]
)

# flat/hybrid name -> canonical dotted name
TRIP_UPDATES_RENAME = {
    "trip_id": "trip_update.trip.trip_id",
    "route_id": "trip_update.trip.route_id",
    "direction_id": "trip_update.trip.direction_id",
    "start_date": "trip_update.trip.start_date",
    "start_time": "trip_update.trip.start_time",
    "schedule_relationship": "trip_update.trip.schedule_relationship",
    "vehicle_id": "trip_update.vehicle.id",
    "vehicle_label": "trip_update.vehicle.label",
    "stop_sequence": "trip_update.stop_time_update.stop_sequence",
    "stop_id": "trip_update.stop_time_update.stop_id",
    "arrival_delay": "trip_update.stop_time_update.arrival.delay",
    "arrival_time": "trip_update.stop_time_update.arrival.time",
    "arrival_uncertainty": "trip_update.stop_time_update.arrival.uncertainty",
    "departure_delay": "trip_update.stop_time_update.departure.delay",
    "departure_time": "trip_update.stop_time_update.departure.time",
    "departure_uncertainty": "trip_update.stop_time_update.departure.uncertainty",
    "stop_time_schedule_relationship": "trip_update.stop_time_update.schedule_relationship",
}

VEHICLES_SCHEMA = pa.schema(
    [
        pa.field("feed_timestamp", pa.int64()),
        pa.field("vehicle.vehicle.id", pa.string()),
        pa.field("vehicle.vehicle.label", pa.string()),
        pa.field("vehicle.trip.trip_id", pa.string()),
        pa.field("vehicle.trip.route_id", pa.string()),
        pa.field("vehicle.trip.direction_id", pa.int64()),
        pa.field("vehicle.trip.start_date", pa.string()),
        pa.field("vehicle.trip.start_time", pa.string()),  # flat-era: missing
        pa.field("vehicle.trip.schedule_relationship", pa.string()),
        pa.field("vehicle.position.latitude", pa.float64()),
        pa.field("vehicle.position.longitude", pa.float64()),
        pa.field("vehicle.position.bearing", pa.float64()),
        pa.field("vehicle.position.speed", pa.float64()),
        pa.field("vehicle.current_stop_sequence", pa.int64()),
        pa.field("vehicle.stop_id", pa.string()),
        pa.field("vehicle.current_status", pa.string()),
        pa.field("vehicle.occupancy_status", pa.string()),
        pa.field("vehicle.occupancy_percentage", pa.int64()),
        pa.field("vehicle.timestamp", pa.int64()),
        pa.field("vehicle.trip.revenue", pa.bool_()),  # flat-era: missing
        pa.field("vehicle.vehicle.consist", _LABEL_LIST),  # flat-era: missing
        pa.field("vehicle.multi_carriage_details", _LABEL_LIST),  # flat-era: missing
    ]
)

VEHICLES_RENAME = {
    "vehicle_id": "vehicle.vehicle.id",
    "vehicle_label": "vehicle.vehicle.label",
    "trip_id": "vehicle.trip.trip_id",
    "route_id": "vehicle.trip.route_id",
    "direction_id": "vehicle.trip.direction_id",
    "start_date": "vehicle.trip.start_date",
    "start_time": "vehicle.trip.start_time",
    "schedule_relationship": "vehicle.trip.schedule_relationship",
    "latitude": "vehicle.position.latitude",
    "longitude": "vehicle.position.longitude",
    "bearing": "vehicle.position.bearing",
    "speed": "vehicle.position.speed",
    "current_stop_sequence": "vehicle.current_stop_sequence",
    "stop_id": "vehicle.stop_id",
    "current_status": "vehicle.current_status",
    "occupancy_status": "vehicle.occupancy_status",
    "occupancy_percentage": "vehicle.occupancy_percentage",
    "vehicle_timestamp": "vehicle.timestamp",
}

DATASETS = {
    "trip_updates": (TRIP_UPDATES_SCHEMA, TRIP_UPDATES_RENAME),
    "vehicles": (VEHICLES_SCHEMA, VEHICLES_RENAME),
}


def classify(names: list[str]) -> str:
    """FLAT / HYBRID / DOTTED, by whether dotted protobuf paths are present."""
    dotted = [n for n in names if "." in n]
    if not dotted:
        return "FLAT"
    if len(dotted) == len(names) - 1:  # all but feed_timestamp are dotted
        return "DOTTED"
    return "HYBRID"


def migrate_table(
    table: pa.Table, schema: pa.Schema, rename: dict[str, str]
) -> pa.Table:
    """Rename flat names to dotted, null-pad missing canonical columns, reorder."""
    new_names = [rename.get(n, n) for n in table.column_names]
    table = table.rename_columns(new_names)
    present = set(table.column_names)
    n = table.num_rows
    arrays = []
    for field in schema:
        if field.name in present:
            col = table.column(field.name)
            if col.type != field.type:
                col = col.cast(field.type)
            arrays.append(col)
        else:
            arrays.append(pa.nulls(n, type=field.type))
    return pa.table(arrays, schema=schema)


def _compression(path: Path) -> str:
    """Preserve the source file's compression codec; default snappy."""
    try:
        md = pq.ParquetFile(path).metadata
        if md.num_row_groups and md.row_group(0).num_columns:
            return md.row_group(0).column(0).compression.lower()
    except Exception:
        pass
    return "snappy"


def migrate_file(
    path: Path, schema: pa.Schema, rename: dict[str, str], apply: bool
) -> str:
    """Migrate one parquet file in place. Returns the action taken."""
    names = [f.name for f in pq.read_schema(path)]
    era = classify(names)
    if era == "DOTTED":
        return "skip"
    if not apply:
        return f"would-migrate:{era}"

    table = migrate_table(pq.read_table(path), schema, rename)
    tmp = path.with_suffix(path.suffix + ".tmp-migrate")
    pq.write_table(table, tmp, compression=_compression(path))
    os.replace(tmp, path)
    return f"migrated:{era}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--curated-dir", type=Path, default=Path("curated"))
    p.add_argument(
        "--dataset",
        choices=[*DATASETS, "both"],
        default="both",
        help="which dataset to migrate (default: both)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually rewrite files (default: dry run, report only)",
    )
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    names = list(DATASETS) if args.dataset == "both" else [args.dataset]

    for ds in names:
        schema, rename = DATASETS[ds]
        root = args.curated_dir / ds
        files = sorted(root.rglob("*.parquet"))
        if not files:
            print(f"{ds}: no parquet files under {root}", file=sys.stderr)
            continue

        def work(f: Path) -> str:
            try:
                return migrate_file(f, schema, rename, args.apply)
            except Exception as e:  # noqa: BLE001 - report and continue
                return f"error:{type(e).__name__}: {e} [{f}]"

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(work, files))

        tally: dict[str, int] = {}
        errors = []
        for r in results:
            key = r.split(":", 1)[0] if r.startswith("error") else r
            tally[key] = tally.get(key, 0) + 1
            if r.startswith("error"):
                errors.append(r)

        verb = "applied" if args.apply else "dry run"
        print(f"\n{ds}: {len(files)} files ({verb})")
        for k in sorted(tally):
            print(f"  {k:<22} {tally[k]}")
        for e in errors[:20]:
            print(f"  {e}", file=sys.stderr)

    if not args.apply:
        print("\n(dry run — re-run with --apply to rewrite)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
