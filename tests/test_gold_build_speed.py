"""Integration test for gold.build_one's segment-speed mart path.

Exercises the full pipeline: fake vehicle parquet → build_one → segment_speed /
segment_day parquet on disk. The pure computation layer is covered in
test_analysis_segment_speed.py; this test verifies the I/O wiring in gold.py.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

import gold
from analysis.segment_speed import SEGMENT_DAY_SCHEMA, SEGMENT_SPEED_SCHEMA
from analysis.static_gtfs import StaticGtfs
from gold import _mart_path, _partition_path

EASTERN = ZoneInfo("America/New_York")
FEED = "test-vehicles"
DAY = dt.date(2024, 5, 20)
# 2024-05-20 12:00:00 EDT
NOON = 1_716_220_800

# Two stops ~1.25 km apart in Manhattan (Times Sq / 34th St Penn).
_STOPS_CSV = (
    "stop_id,stop_lat,stop_lon,stop_name\n"
    "S1,40.7580,-73.9855,Times Square\n"
    "S2,40.7506,-73.9971,34th St Penn\n"
)


def _gtfs(tmp_path: Path) -> StaticGtfs:
    zp = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("stops.txt", _STOPS_CSV)
    return StaticGtfs(zp)


def _write_vehicles(curated: Path) -> None:
    """Write four pings: two STOPPED_AT S1, then two STOPPED_AT S2 (one trip)."""
    rows = [
        {
            "vehicle.vehicle.id": "V1",
            "vehicle.trip.trip_id": "T1",
            "vehicle.trip.route_id": "R1",
            "vehicle.trip.direction_id": 0,
            "vehicle.stop_id": "S1",
            "vehicle.current_status": "STOPPED_AT",
            "vehicle.timestamp": NOON,
            "feed_timestamp": NOON,
        },
        {
            "vehicle.vehicle.id": "V1",
            "vehicle.trip.trip_id": "T1",
            "vehicle.trip.route_id": "R1",
            "vehicle.trip.direction_id": 0,
            "vehicle.stop_id": "S1",
            "vehicle.current_status": "STOPPED_AT",
            "vehicle.timestamp": NOON + 30,
            "feed_timestamp": NOON + 30,
        },
        {
            "vehicle.vehicle.id": "V1",
            "vehicle.trip.trip_id": "T1",
            "vehicle.trip.route_id": "R1",
            "vehicle.trip.direction_id": 0,
            "vehicle.stop_id": "S2",
            "vehicle.current_status": "STOPPED_AT",
            "vehicle.timestamp": NOON + 630,
            "feed_timestamp": NOON + 630,
        },
        {
            "vehicle.vehicle.id": "V1",
            "vehicle.trip.trip_id": "T1",
            "vehicle.trip.route_id": "R1",
            "vehicle.trip.direction_id": 0,
            "vehicle.stop_id": "S2",
            "vehicle.current_status": "STOPPED_AT",
            "vehicle.timestamp": NOON + 660,
            "feed_timestamp": NOON + 660,
        },
    ]
    path = _partition_path(curated / "vehicles", FEED, DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_build_one_writes_speed_marts(tmp_path):
    curated = tmp_path / "curated"
    _write_vehicles(curated)
    gtfs = _gtfs(tmp_path)

    result = gold.build_one(
        FEED,
        DAY,
        EASTERN,
        curated,
        "vehicles",
        merge_gap_seconds=60,
        force=False,
        gtfs_for=lambda f, d: gtfs,
    )

    assert result is not None
    assert result["segments"] >= 1

    speed_path = _mart_path(curated, "segment_speed", FEED, DAY)
    day_path = _mart_path(curated, "segment_day", FEED, DAY)
    assert speed_path.exists(), f"segment_speed parquet missing: {speed_path}"
    assert day_path.exists(), f"segment_day parquet missing: {day_path}"

    speed_table = pq.read_table(speed_path)
    day_table = pq.read_table(day_path)

    assert len(speed_table) == result["segments"]
    assert len(day_table) >= 1

    # Schema compatibility — same check as test_analysis_segment_speed.py
    pa.Table.from_pylist(speed_table.to_pylist(), schema=SEGMENT_SPEED_SCHEMA)
    pa.Table.from_pylist(day_table.to_pylist(), schema=SEGMENT_DAY_SCHEMA)
