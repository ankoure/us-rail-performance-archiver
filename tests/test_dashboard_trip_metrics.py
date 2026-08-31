"""Unit tests for dashboard/api trip_metrics.compute.

Exercise the folding logic on a tiny synthetic events table — no S3, no
parquet — so trip matching, direction filtering, loop-route handling, headway
derivation and the per-day rollup are pinned independently of the I/O path.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

# `api` is the package at dashboard/api, so its parent goes on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from api.services import trip_metrics  # noqa: E402

DAY = date(2026, 8, 29)
A, B = "STOP_A", "STOP_B"


def _events(rows: list[dict]) -> pa.Table:
    defaults = {"direction_id": 0, "stop_sequence": None, "vehicle_id": "V1"}
    return pa.Table.from_pylist([{**defaults, **row} for row in rows])


def _event(trip: str, stop: str, kind: str, ts: int, **extra) -> dict:
    return {
        "trip_id": trip,
        "stop_id": stop,
        "event_type": kind,
        "event_unix": ts,
        "service_date": "2026-08-29",
        **extra,
    }


@pytest.fixture
def fake_read(monkeypatch):
    """Swap out the S3-backed reader for a caller-supplied table."""

    def _install(table: pa.Table):
        monkeypatch.setattr(trip_metrics.data, "read_kind", lambda *a, **k: table)

    return _install


def _compute(**overrides):
    kwargs = {
        "feed_names": ["f"],
        "route_id": "R",
        "from_stop_id": A,
        "to_stop_id": B,
        "start_date": DAY,
        "end_date": DAY,
    }
    kwargs.update(overrides)
    return trip_metrics.compute(**kwargs)


def test_travel_time_is_departure_at_origin_to_arrival_at_destination(fake_read):
    fake_read(
        _events(
            [
                _event("t1", A, "ARR", 1000),
                _event("t1", A, "DEP", 1030),
                _event("t1", B, "ARR", 1600),
            ]
        )
    )
    (run,) = _compute()["runs"]
    assert run["departure_unix"] == 1030
    assert run["arrival_unix"] == 1600
    assert run["travel_time_s"] == 570


def test_trip_that_never_reaches_destination_is_dropped(fake_read):
    fake_read(_events([_event("t1", A, "DEP", 1000)]))
    assert _compute()["runs"] == []


def test_reversed_pair_yields_nothing(fake_read):
    """B is reached before A on this trip, so A->B never happened."""
    fake_read(
        _events(
            [
                _event("t1", B, "ARR", 1000),
                _event("t1", A, "DEP", 2000),
            ]
        )
    )
    assert _compute()["runs"] == []


def test_loop_route_uses_first_arrival_after_departure(fake_read):
    """A trip can serve the destination twice; only the visit that follows the
    origin departure is this rider's journey."""
    fake_read(
        _events(
            [
                _event("t1", B, "ARR", 500),
                _event("t1", A, "DEP", 1000),
                _event("t1", B, "ARR", 1400),
                _event("t1", B, "ARR", 1900),
            ]
        )
    )
    (run,) = _compute()["runs"]
    assert run["arrival_unix"] == 1400
    assert run["travel_time_s"] == 400


def test_direction_filter_excludes_other_direction(fake_read):
    fake_read(
        _events(
            [
                _event("t1", A, "DEP", 1000, direction_id=0),
                _event("t1", B, "ARR", 1600, direction_id=0),
                _event("t2", A, "DEP", 2000, direction_id=1),
                _event("t2", B, "ARR", 2600, direction_id=1),
            ]
        )
    )
    runs = _compute(direction_id=1)["runs"]
    assert [r["trip_id"] for r in runs] == ["t2"]


def test_headway_is_gap_to_previous_arrival_at_origin(fake_read):
    fake_read(
        _events(
            [
                _event("t1", A, "ARR", 1000),
                _event("t1", A, "DEP", 1000),
                _event("t1", B, "ARR", 1500),
                _event("t2", A, "ARR", 1300),
                _event("t2", A, "DEP", 1300),
                _event("t2", B, "ARR", 1900),
            ]
        )
    )
    runs = _compute()["runs"]
    assert runs[0]["headway_s"] is None, "first vehicle of the day has no predecessor"
    assert runs[1]["headway_s"] == 300


def test_runs_are_ordered_by_departure(fake_read):
    fake_read(
        _events(
            [
                _event("late", A, "DEP", 5000),
                _event("late", B, "ARR", 5600),
                _event("early", A, "DEP", 1000),
                _event("early", B, "ARR", 1600),
            ]
        )
    )
    assert [r["trip_id"] for r in _compute()["runs"]] == ["early", "late"]


def test_days_rollup_splits_by_service_date(fake_read):
    fake_read(
        _events(
            [
                _event("t1", A, "DEP", 1000),
                _event("t1", B, "ARR", 1600),
                _event("t2", A, "DEP", 90000, service_date="2026-08-30"),
                _event("t2", B, "ARR", 90900, service_date="2026-08-30"),
            ]
        )
    )
    days = _compute(end_date=date(2026, 8, 30))["days"]
    assert [d["service_date"] for d in days] == ["2026-08-29", "2026-08-30"]
    assert [d["trip_count"] for d in days] == [1, 1]
    assert days[0]["travel_time_p50_s"] == 600
    assert days[1]["travel_time_p50_s"] == 900


def test_percentiles_of_a_single_run_are_that_run(fake_read):
    """statistics.quantiles needs two points; one run must not raise."""
    fake_read(_events([_event("t1", A, "DEP", 1000), _event("t1", B, "ARR", 1600)]))
    (day,) = _compute()["days"]
    assert day["travel_time_p10_s"] == 600
    assert day["travel_time_p50_s"] == 600
    assert day["travel_time_p90_s"] == 600
    assert day["headway_p50_s"] is None
