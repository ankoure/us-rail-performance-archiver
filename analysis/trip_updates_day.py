"""Build Visits from GTFS-RT trip_updates instead of vehicle positions.

Some agencies publish vehicle positions without stop_id or current_status for
certain modes (Metro Transit MN does this for its light rail) but DO publish
per-stop predictions in their trip_updates feed. TripUpdatesDay reads the
curated trip_updates parquet and synthesizes a Visit per (trip_id, stop_id)
by keeping the prediction with the latest feed_timestamp — the prediction
made closest to the actual stop visit, since agencies refine predictions up
until the vehicle reaches the stop and then drop it from the StopTimeUpdate
list.

Output Visits are the same dataclass [[Visit]] that VehicleDay emits, and the
class exposes a `.vehicles` list of stub objects with a `.dwells` property so
[[export_events_csv]] can consume either source via duck-typing — no changes
required in the exporter.

Caveat: when arrival.time is absent from the feed (common for light rail,
where dwells are too brief for the agency to bother predicting separately)
arrival_ts is set equal to departure_ts. Downstream tools see dwell == 0.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from analysis.vehicle_day import DEFAULT_CURATED_DIR, Visit, _coerce_date

# Curated trip_updates parquet uses dotted protobuf paths (matching
# StopTimeUpdateRow's TableSpec.column_names in archiver/decoder.py). Map them
# to flat internal names.
_TU_COLUMN_MAP = {
    "trip_update.trip.trip_id": "trip_id",
    "trip_update.trip.route_id": "route_id",
    "trip_update.trip.direction_id": "direction_id",
    "trip_update.stop_time_update.stop_id": "stop_id",
    "trip_update.stop_time_update.stop_sequence": "stop_sequence",
    "trip_update.stop_time_update.arrival.time": "arrival_time",
    "trip_update.stop_time_update.departure.time": "departure_time",
    "trip_update.stop_time_update.schedule_relationship": "stop_schedule_relationship",
    "trip_update.vehicle.id": "vehicle_id",
    "trip_update.vehicle.label": "vehicle_label",
    "feed_timestamp": "feed_timestamp",
}


@dataclass(slots=True)
class _StubVehicle:
    """Stand-in for [[Vehicle]] when visits come from trip_updates.

    Carries a pre-built list of Visits and exposes them under `.dwells`, which
    is the only attribute event_export iterates on.
    """

    vehicle_id: str
    dwells: list[Visit]


class TripUpdatesDay:
    """Per-(feed, day) loader for trip_updates that mirrors the [[VehicleDay]] API.

    Plug-compatible with the event exporter: exposes `.feed`, `.vehicles`
    (list of stub vehicles), and each vehicle exposes `.dwells`. The
    `merge_gap_seconds` kwarg is accepted but ignored — trip_updates already
    produce one row per (trip, stop), so there's nothing to merge.
    """

    def __init__(
        self,
        feed: str,
        date: dt.date | str,
        base_dir: Path | str = DEFAULT_CURATED_DIR,
        merge_gap_seconds: int = 0,  # accepted for API parity, unused
    ) -> None:
        self.feed = feed
        self.date = _coerce_date(date)
        self.base_dir = Path(base_dir)
        self.merge_gap_seconds = merge_gap_seconds

    def __repr__(self) -> str:
        return f"TripUpdatesDay(feed={self.feed!r}, date={self.date.isoformat()})"

    @property
    def partition_path(self) -> Path:
        return (
            self.base_dir
            / "trip_updates"
            / f"feed={self.feed}"
            / f"year={self.date.year}"
            / f"month={self.date.month}"
            / f"day={self.date.day}"
        )

    @cached_property
    def visits(self) -> list[Visit]:
        """Synthesized Visits — one per (trip_id, stop_id), latest prediction wins.

        Pipeline is two-stage so the dedup stays in arrow space:
          1. Read each row group projected to needed columns; rename to
             internal names; filter junk rows; sort by feed_timestamp desc;
             group_by (trip_id, stop_id) keeping 'first' — gives one row per
             key with the largest feed_timestamp inside that row group.
          2. Concat all partials and repeat the sort+group_by once — collapses
             duplicates that crossed row-group boundaries.
        """
        path = self.partition_path
        if not path.exists():
            raise FileNotFoundError(f"No trip_updates partition at {path}")
        files = sorted(path.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No .parquet files under {path}")

        partials: list[pa.Table] = []
        for pf_path in files:
            pf = pq.ParquetFile(pf_path)
            present = [c for c in _TU_COLUMN_MAP if c in pf.schema_arrow.names]
            for i in range(pf.num_row_groups):
                rg = pf.read_row_group(i, columns=present)
                rg = _normalize(rg)
                partial = _dedupe_latest_per_key(rg)
                if partial.num_rows > 0:
                    partials.append(partial)

        if not partials:
            return []
        combined = pa.concat_tables(partials, promote_options="default")
        final = _dedupe_latest_per_key(combined)
        return _table_to_visits(final)

    @cached_property
    def vehicles(self) -> list[_StubVehicle]:
        """Group Visits by vehicle_id so [[export_events_csv]]'s iteration works.

        Visits that lack a vehicle_id are bucketed under a synthetic '_unknown'
        vehicle. The grouping is purely for iteration shape; events.csv carries
        vehicle_label per-row, not per-vehicle.
        """
        groups: dict[str, list[Visit]] = defaultdict(list)
        for v in self.visits:
            groups[v.vehicle_id or "_unknown"].append(v)
        return [_StubVehicle(vid, dwells) for vid, dwells in groups.items()]


# Internal column order — used to fill in missing columns with nulls so every
# row group flows through _dedupe_latest_per_key with the same schema.
_INTERNAL_COLS: tuple[str, ...] = (
    "trip_id",
    "stop_id",
    "feed_timestamp",
    "arrival_time",
    "departure_time",
    "stop_schedule_relationship",
    "route_id",
    "direction_id",
    "stop_sequence",
    "vehicle_id",
    "vehicle_label",
)
_KEY_COLS: tuple[str, str] = ("trip_id", "stop_id")
_NULLABLE_TYPES: dict[str, pa.DataType] = {
    "trip_id": pa.string(),
    "stop_id": pa.string(),
    "feed_timestamp": pa.int64(),
    "arrival_time": pa.int64(),
    "departure_time": pa.int64(),
    "stop_schedule_relationship": pa.string(),
    "route_id": pa.string(),
    "direction_id": pa.int64(),
    "stop_sequence": pa.int64(),
    "vehicle_id": pa.string(),
    "vehicle_label": pa.string(),
}


def _normalize(rg: pa.Table) -> pa.Table:
    """Rename curated columns to internal names and fill in any missing ones.

    Output schema is always [[_INTERNAL_COLS]] in order with the declared
    type per column. Adds null-filled columns for missing inputs and casts
    incoming columns of type `null` (what pyarrow infers when every value
    in a column happens to be None) to their declared types so the
    downstream group_by('first') aggregator has a typed kernel to dispatch.
    """
    rename = {src: dst for src, dst in _TU_COLUMN_MAP.items() if src in rg.column_names}
    rg = rg.select(list(rename.keys())).rename_columns(list(rename.values()))
    n = rg.num_rows
    for name in _INTERNAL_COLS:
        declared = _NULLABLE_TYPES[name]
        if name not in rg.column_names:
            rg = rg.append_column(name, pa.nulls(n, type=declared))
        elif rg.column(name).type == pa.null():
            idx = rg.column_names.index(name)
            rg = rg.set_column(idx, name, pa.nulls(n, type=declared))
    return rg.select(list(_INTERNAL_COLS))


def _dedupe_latest_per_key(table: pa.Table) -> pa.Table:
    """Keep the row with the largest feed_timestamp per (trip_id, stop_id).

    Filters out junk rows (missing key/timestamp, SKIPPED/NO_DATA stop
    schedule relationships, neither arrival nor departure time populated)
    before sorting so the sort+group_by only handles real candidates.
    """
    if table.num_rows == 0:
        return table

    mask = pc.and_(pc.is_valid(table["trip_id"]), pc.is_valid(table["stop_id"]))
    mask = pc.and_(mask, pc.is_valid(table["feed_timestamp"]))
    # Treat null schedule_relationship as SCHEDULED (per GTFS-RT spec): fill_null
    # collapses arrow's 3-valued bool to 2-valued so the AND below doesn't drop
    # the row when the equality returns null on a null input.
    sched_ok = pc.fill_null(
        pc.equal(table["stop_schedule_relationship"], "SCHEDULED"), True
    )
    mask = pc.and_(mask, sched_ok)
    has_time = pc.or_(
        pc.is_valid(table["arrival_time"]),
        pc.is_valid(table["departure_time"]),
    )
    mask = pc.and_(mask, has_time)
    filtered = table.filter(mask)
    if filtered.num_rows == 0:
        return filtered

    sorted_tbl = filtered.sort_by([("feed_timestamp", "descending")])
    val_cols = [c for c in sorted_tbl.column_names if c not in _KEY_COLS]
    # use_threads=False is required: pyarrow 24's group_by with 'first' is an
    # ordered aggregator and refuses to run multi-threaded.
    deduped = sorted_tbl.group_by(list(_KEY_COLS), use_threads=False).aggregate(
        [(c, "first") for c in val_cols]
    )
    # 'first' aggregation suffixes value columns with '_first' — strip it back.
    new_names = [
        c if c in _KEY_COLS else c[: -len("_first")] for c in deduped.column_names
    ]
    return deduped.rename_columns(new_names)


def _table_to_visits(table: pa.Table) -> list[Visit]:
    """Convert the deduped arrow table to Visit dataclasses in one pass."""
    if table.num_rows == 0:
        return []
    cols = {c: table.column(c).to_pylist() for c in table.column_names}
    visits: list[Visit] = []
    for j in range(table.num_rows):
        arr = cols["arrival_time"][j]
        dep = cols["departure_time"][j]
        if arr is None:
            arr = dep
        if dep is None:
            dep = arr
        vid = cols["vehicle_id"][j] or cols["vehicle_label"][j]
        direction = cols["direction_id"][j]
        stop_seq = cols["stop_sequence"][j]
        visits.append(
            Visit(
                vehicle_id=vid or "",
                stop_id=cols["stop_id"][j],
                arrival_ts=int(arr),
                departure_ts=int(dep),
                ping_count=1,
                route_id=cols["route_id"][j],
                trip_id=cols["trip_id"][j],
                direction_id=int(direction) if direction is not None else None,
                stop_sequence=int(stop_seq) if stop_seq is not None else None,
            )
        )
    return visits
