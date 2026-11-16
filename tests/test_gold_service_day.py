"""gold builds marts per local service day, not per UTC silver partition.

Regression for issue #118: a non-UTC feed's UTC-day mart files each held two
partial service_dates, so the API returned two partial rows per
(route, stop, service_date).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from analysis.timeutil import service_day_bounds
from analysis.trip_updates_day import TripUpdatesDay, _merge_partitions
from analysis.vehicle_day import Visit
from pipeline import gold
from pipeline.gold import _mart_path, _partition_path

NY = ZoneInfo("America/New_York")
FEED = "test-vehicles"
SERVICE_DAY = dt.date(2026, 6, 10)
HEADWAY = 600


def _ping(ts: int, i: int) -> dict:
    return {
        "vehicle.vehicle.id": f"V{i}",
        "vehicle.trip.trip_id": f"T{i}",
        "vehicle.trip.route_id": "R1",
        "vehicle.trip.direction_id": 0,
        "vehicle.stop_id": "S1",
        "vehicle.current_status": "STOPPED_AT",
        "vehicle.timestamp": ts,
        "feed_timestamp": ts,
    }


def _write_by_utc_day(curated: Path, table: str, rows: list[dict], ts_col: str):
    by_day: dict[dt.date, list[dict]] = {}
    for r in rows:
        day = dt.datetime.fromtimestamp(r[ts_col], tz=dt.timezone.utc).date()
        by_day.setdefault(day, []).append(r)
    for day, day_rows in by_day.items():
        path = _partition_path(curated / table, FEED, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(day_rows), path)


def _build(curated: Path, source: str = "vehicles", day=SERVICE_DAY):
    return gold.build_one(FEED, day, NY, curated, source, 60, False)


def _read(curated: Path, mart: str, day=SERVICE_DAY) -> list[dict]:
    return pq.read_table(_mart_path(curated, mart, FEED, day)).to_pylist()


def test_one_row_per_stop_from_every_overlapping_utc_partition(tmp_path, capsys):
    curated = tmp_path / "curated"
    # Arrivals every 10 min from UTC 06-10 00:00 to 06-12 00:00, spread over
    # two UTC partitions; service day 06-10 is 06-10 07:00Z to 06-11 07:00Z.
    first = int(dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc).timestamp())
    stamps = list(range(first, first + 2 * 86400, HEADWAY))
    _write_by_utc_day(
        curated,
        "vehicles",
        [_ping(ts, i) for i, ts in enumerate(stamps)],
        "vehicle.timestamp",
    )
    start, end = service_day_bounds(SERVICE_DAY, NY)
    in_day = [ts for ts in stamps if start <= ts < end]

    result = _build(curated)

    assert result["stop"] == 1 and result["route"] == 1
    (row,) = _read(curated, "stop_day")
    assert row["service_date"] == "2026-06-10"
    assert row["visit_count"] == len(in_day) == 24 * 3600 // HEADWAY
    assert row["first_service_unix"] == start
    assert row["last_service_unix"] == in_day[-1]
    # Every headway counted, including the one across UTC midnight.
    assert row["headway_mean_s"] == HEADWAY
    (route,) = _read(curated, "route_day")
    assert route["service_date"] == "2026-06-10"
    assert route["visit_count"] == len(in_day)
    events = _read(curated, "events")
    assert {e["service_date"] for e in events} == {"2026-06-10"}
    assert len(events) == 2 * len(in_day)
    assert "partial service day" not in capsys.readouterr().err


def test_dwell_across_utc_midnight_is_one_visit(tmp_path):
    curated = tmp_path / "curated"
    midnight = int(dt.datetime(2026, 6, 11, tzinfo=dt.timezone.utc).timestamp())
    pings = [_ping(midnight - 30, 0), _ping(midnight + 30, 0)]
    _write_by_utc_day(curated, "vehicles", pings, "vehicle.timestamp")

    _build(curated)

    (row,) = _read(curated, "stop_day")
    assert row["visit_count"] == 1
    assert row["dwell_p50_s"] == 60


def test_missing_partition_warns_partial_day(tmp_path, capsys):
    curated = tmp_path / "curated"
    noon = int(dt.datetime(2026, 6, 10, 16, tzinfo=dt.timezone.utc).timestamp())
    _write_by_utc_day(curated, "vehicles", [_ping(noon, 0)], "vehicle.timestamp")

    assert _build(curated)["stop"] == 1
    assert "partial service day, no silver for UTC 2026-06-11" in (
        capsys.readouterr().err
    )


def test_service_days_to_build_maps_nightly_utc_day():
    assert gold.service_days_to_build(
        NY, utc_days=[dt.date(2026, 6, 11)], service_dates=[dt.date(2026, 6, 1)]
    ) == [dt.date(2026, 6, 1), dt.date(2026, 6, 10)]


# --- trip_updates across UTC partitions ----------------------------------- #


def _tu_row(trip, stop, arrival, feed_ts):
    return {
        "trip_update.trip.trip_id": trip,
        "trip_update.trip.route_id": "R1",
        "trip_update.stop_time_update.stop_id": stop,
        "trip_update.stop_time_update.arrival.time": arrival,
        "trip_update.stop_time_update.departure.time": arrival,
        "feed_timestamp": feed_ts,
    }


def test_trip_updates_keeps_recurring_trip_id_and_latest_cross_midnight_prediction(
    tmp_path,
):
    curated = tmp_path / "curated"
    utc_mid = int(dt.datetime(2026, 6, 11, tzinfo=dt.timezone.utc).timestamp())
    day = 86400
    rows = [
        # Same trip_id runs at 13:00 local on both days. These are two visits,
        # not one to dedupe.
        _tu_row("DAILY", "S1", utc_mid - 7 * 3600, utc_mid - 7 * 3600 - 60),
        _tu_row("DAILY", "S1", utc_mid - 7 * 3600 + day, utc_mid - 7 * 3600 + day - 60),
        # One stop event predicted on both sides of UTC midnight: the later
        # prediction (in the 06-11 partition) wins.
        _tu_row("NIGHT", "S2", utc_mid + 300, utc_mid - 600),
        _tu_row("NIGHT", "S2", utc_mid + 420, utc_mid + 400),
    ]
    _write_by_utc_day(curated, "trip_updates", rows, "feed_timestamp")

    tud = TripUpdatesDay(
        FEED,
        SERVICE_DAY,
        base_dir=curated,
        partitions=[dt.date(2026, 6, 10), dt.date(2026, 6, 11)],
    )
    got = sorted((v.trip_id, v.arrival_ts) for v in tud.visits)
    assert got == [
        ("DAILY", utc_mid - 7 * 3600),
        ("DAILY", utc_mid - 7 * 3600 + day),
        ("NIGHT", utc_mid + 420),
    ]


def test_merge_partitions_single_partition_passthrough():
    v = Visit("V", "S", 1, 1, 1, trip_id="T")
    assert _merge_partitions([[v]]) == [v]
