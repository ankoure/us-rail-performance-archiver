"""Tests for pipeline/compact_trip_updates.py.

compact_table() is meant to reduce a curated trip_updates table to exactly
what analysis.trip_updates_day.TripUpdatesDay._dedupe_latest_per_key already
extracts from it — same junk filter, same latest-wins selection — just over
the full curated schema instead of that method's internal column subset.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.compact_trip_updates import compact_one, compact_table

_SCHEMA = pa.schema(
    [
        ("feed_timestamp", pa.int64()),
        ("trip_update.timestamp", pa.int64()),
        ("trip_update.trip.trip_id", pa.string()),
        ("trip_update.trip.route_id", pa.string()),
        ("trip_update.trip.direction_id", pa.int64()),
        ("trip_update.trip.start_date", pa.string()),
        ("trip_update.trip.start_time", pa.string()),
        ("trip_update.trip.schedule_relationship", pa.string()),
        ("trip_update.vehicle.id", pa.string()),
        ("trip_update.vehicle.label", pa.string()),
        ("trip_update.stop_time_update.stop_sequence", pa.int64()),
        ("trip_update.stop_time_update.stop_id", pa.string()),
        ("trip_update.stop_time_update.arrival.delay", pa.int64()),
        ("trip_update.stop_time_update.arrival.time", pa.int64()),
        ("trip_update.stop_time_update.arrival.uncertainty", pa.int64()),
        ("trip_update.stop_time_update.departure.delay", pa.int64()),
        ("trip_update.stop_time_update.departure.time", pa.int64()),
        ("trip_update.stop_time_update.departure.uncertainty", pa.int64()),
        ("trip_update.stop_time_update.schedule_relationship", pa.string()),
    ]
)
_SCHEMA_COLS = _SCHEMA.names


def _row(
    *,
    trip_id: str | None = "T1",
    stop_id: str | None = "S1",
    feed_timestamp: int | None = 1_700_000_000,
    departure_time: int | None = 1_700_000_000,
    arrival_time: int | None = None,
    sched_rel: str | None = "SCHEDULED",
    route_id: str = "901",
) -> dict:
    return {
        "feed_timestamp": feed_timestamp,
        "trip_update.timestamp": feed_timestamp,
        "trip_update.trip.trip_id": trip_id,
        "trip_update.trip.route_id": route_id,
        "trip_update.trip.direction_id": 0,
        "trip_update.trip.start_date": "20260101",
        "trip_update.trip.start_time": "08:00:00",
        "trip_update.trip.schedule_relationship": "SCHEDULED",
        "trip_update.vehicle.id": "V1",
        "trip_update.vehicle.label": None,
        "trip_update.stop_time_update.stop_sequence": 1,
        "trip_update.stop_time_update.stop_id": stop_id,
        "trip_update.stop_time_update.arrival.delay": None,
        "trip_update.stop_time_update.arrival.time": arrival_time,
        "trip_update.stop_time_update.arrival.uncertainty": None,
        "trip_update.stop_time_update.departure.delay": None,
        "trip_update.stop_time_update.departure.time": departure_time,
        "trip_update.stop_time_update.departure.uncertainty": None,
        "trip_update.stop_time_update.schedule_relationship": sched_rel,
    }


def _table(rows: list[dict]) -> pa.Table:
    cols: dict[str, list] = {c: [r.get(c) for r in rows] for c in _SCHEMA_COLS}
    return pa.table(cols, schema=_SCHEMA)


class TestLatestWins:
    def test_keeps_latest_feed_timestamp_per_trip_stop(self):
        rows = [
            _row(feed_timestamp=1000, departure_time=995),
            _row(feed_timestamp=1020, departure_time=1005),
            _row(feed_timestamp=1010, departure_time=1003),  # out of order
        ]
        result = compact_table(_table(rows))
        assert result.num_rows == 1
        assert (
            result.column("trip_update.stop_time_update.departure.time")[0].as_py()
            == 1005
        )

    def test_different_stops_kept_separately(self):
        rows = [_row(stop_id="S1"), _row(stop_id="S2")]
        result = compact_table(_table(rows))
        assert result.num_rows == 2

    def test_different_trips_kept_separately(self):
        rows = [_row(trip_id="T1"), _row(trip_id="T2")]
        result = compact_table(_table(rows))
        assert result.num_rows == 2

    def test_output_schema_matches_input(self):
        result = compact_table(_table([_row()]))
        assert result.column_names == _SCHEMA_COLS


class TestJunkFiltering:
    def test_missing_trip_id_dropped(self):
        result = compact_table(_table([_row(trip_id=None)]))
        assert result.num_rows == 0

    def test_missing_stop_id_dropped(self):
        result = compact_table(_table([_row(stop_id=None)]))
        assert result.num_rows == 0

    def test_missing_feed_timestamp_dropped(self):
        result = compact_table(_table([_row(feed_timestamp=None)]))
        assert result.num_rows == 0

    def test_skipped_schedule_relationship_dropped(self):
        result = compact_table(_table([_row(sched_rel="SKIPPED")]))
        assert result.num_rows == 0

    def test_no_data_schedule_relationship_dropped(self):
        result = compact_table(_table([_row(sched_rel="NO_DATA")]))
        assert result.num_rows == 0

    def test_null_schedule_relationship_treated_as_scheduled(self):
        # Per GTFS-RT spec, an absent schedule_relationship means SCHEDULED.
        result = compact_table(
            _table([_row(sched_rel=None, arrival_time=1_700_000_100)])
        )
        assert result.num_rows == 1

    def test_neither_arrival_nor_departure_dropped(self):
        result = compact_table(_table([_row(arrival_time=None, departure_time=None)]))
        assert result.num_rows == 0

    def test_latest_junk_does_not_win_over_earlier_real_row(self):
        # The critical case: a later poll marks the stop SKIPPED, but an
        # earlier poll had a real prediction. The real one should survive,
        # not get shadowed by the junk row just because it's more recent.
        rows = [
            _row(feed_timestamp=1000, departure_time=995, sched_rel="SCHEDULED"),
            _row(feed_timestamp=2000, departure_time=None, sched_rel="SKIPPED"),
        ]
        result = compact_table(_table(rows))
        assert result.num_rows == 1
        assert (
            result.column("trip_update.stop_time_update.departure.time")[0].as_py()
            == 995
        )


class TestEdgeCases:
    def test_empty_table(self):
        result = compact_table(_table([]))
        assert result.num_rows == 0

    def test_all_junk_drops_whole_group(self):
        rows = [_row(sched_rel="SKIPPED"), _row(sched_rel="NO_DATA")]
        result = compact_table(_table(rows))
        assert result.num_rows == 0

    def test_idempotent(self):
        rows = [
            _row(feed_timestamp=1000, departure_time=995),
            _row(feed_timestamp=2000, departure_time=1005),
        ]
        once = compact_table(_table(rows))
        twice = compact_table(once)
        assert once.num_rows == twice.num_rows == 1
        assert once.column(
            "trip_update.stop_time_update.departure.time"
        ) == twice.column("trip_update.stop_time_update.departure.time")


class TestCompactOne:
    def test_compacts_file_in_place_and_ignores_hive_partition_columns(
        self, tmp_path: Path
    ):
        # Regression test: reading via a real Hive-style partitioned path
        # (feed=/year=/month=/day=) must not let pq.read_table's partition
        # auto-detection inject extra dictionary-encoded feed/year/month/day
        # columns into the output.
        feed, day = "test-feed", dt.date(2026, 5, 22)
        part_dir = (
            tmp_path
            / "trip_updates"
            / f"feed={feed}"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
        )
        part_dir.mkdir(parents=True)
        rows = [
            _row(feed_timestamp=1000, departure_time=995),
            _row(feed_timestamp=2000, departure_time=1005),
        ]
        pq.write_table(_table(rows), part_dir / "data.parquet")

        result = compact_one(feed, day, tmp_path)
        assert result == (2, 1)

        written = pq.ParquetFile(part_dir / "data.parquet").read()
        assert written.column_names == _SCHEMA_COLS
        assert written.num_rows == 1

    def test_missing_partition_returns_none(self, tmp_path: Path):
        result = compact_one("nonexistent-feed", dt.date(2026, 5, 22), tmp_path)
        assert result is None

    def test_already_compact_skips_rewrite(self, tmp_path: Path):
        feed, day = "test-feed", dt.date(2026, 5, 22)
        part_dir = (
            tmp_path
            / "trip_updates"
            / f"feed={feed}"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
        )
        part_dir.mkdir(parents=True)
        pq.write_table(_table([_row()]), part_dir / "data.parquet")

        first = compact_one(feed, day, tmp_path)
        mtime_after_first = (part_dir / "data.parquet").stat().st_mtime_ns
        second = compact_one(feed, day, tmp_path)
        mtime_after_second = (part_dir / "data.parquet").stat().st_mtime_ns

        assert first == (1, 1)
        assert second == (1, 1)
        assert mtime_after_first == mtime_after_second  # no rewrite on no-op
