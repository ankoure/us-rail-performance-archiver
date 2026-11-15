"""Tests for the dashboard API's shared start_date/end_date validation.

Drive the real routers through TestClient with the S3 reader stubbed out, so
the check is pinned at the HTTP boundary every parquet-backed endpoint shares.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# `api` is the package at dashboard/api, so its parent goes on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from api import dependencies  # noqa: E402
from api.routers import route_day, stop_day, trip_metrics  # noqa: E402
from api.services import data  # noqa: E402
from api.services.agencies import Agency  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    calls: list[tuple] = []

    def fake_read_kind(kind, feed_names, start_date, end_date, **_):
        calls.append((kind, start_date, end_date))
        return pa.table({"service_date": pa.array([], pa.string())})

    monkeypatch.setattr(data, "read_kind", fake_read_kind)
    app = fastapi.FastAPI()
    for r in (stop_day, route_day, trip_metrics):
        app.include_router(r.router)
    app.dependency_overrides[dependencies.get_agency] = lambda: Agency(
        agency_id="x", name="X", timezone="UTC", feed_names=["f"]
    )
    c = TestClient(app)
    c.calls = calls
    return c


def test_range_within_limit_is_read(client):
    r = client.get(
        "/agencies/x/stop_day",
        params={"start_date": "2026-01-01", "end_date": "2026-03-31"},
    )
    assert r.status_code == 200
    assert client.calls[0][0] == "stop_day"


def test_max_range_is_inclusive(client):
    r = client.get(
        "/agencies/x/route_day",
        params={"start_date": "2025-01-01", "end_date": "2026-01-02"},  # 366 days
    )
    assert r.status_code == 200


def test_too_wide_range_rejected_before_reading(client):
    r = client.get(
        "/agencies/x/stop_day",
        params={"start_date": "2000-01-01", "end_date": "2100-01-01"},
    )
    assert r.status_code == 400
    assert "Range too wide" in r.json()["detail"]
    assert client.calls == []


def test_reversed_range_rejected(client):
    r = client.get(
        "/agencies/x/route_day",
        params={"start_date": "2026-02-01", "end_date": "2026-01-01"},
    )
    assert r.status_code == 400
    assert "after end_date" in r.json()["detail"]
    assert client.calls == []


def test_events_endpoint_has_tighter_limit(client):
    params = {
        "route_id": "R",
        "from_stop_id": "A",
        "to_stop_id": "B",
        "start_date": "2026-01-01",
        "end_date": "2026-06-01",
    }
    r = client.get("/agencies/x/trip_metrics", params=params)
    assert r.status_code == 400
    assert f"max {dependencies.MAX_EVENTS_RANGE_DAYS} days" in r.json()["detail"]
