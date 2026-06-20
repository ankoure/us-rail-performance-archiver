"""Tests for analysis/normalize.py — the gold-layer id/unit/time normalizers."""

from __future__ import annotations

import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis.static_gtfs import StaticGtfs
from analysis.normalize import (
    NORMALIZED_VEHICLES_SCHEMA,
    Normalizer,
    brta_candidate_short_name,
    combine_clock_with_poll_date,
    iso_offset_to_utc,
    kmh_to_mps,
    mph_to_mps,
    naive_local_to_utc,
)

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def gtfs(tmp_path: Path) -> StaticGtfs:
    """A tiny static GTFS covering every resolution path the normalizers exercise."""
    zp = tmp_path / "feed.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(
            "routes.txt",
            "route_id,route_type,route_short_name,route_long_name\n"
            "R_RT04S,3,RT04S,MetroWest 4\n"
            "R_CC,3,Hyannis,Sealine Hyannis-Falmouth/Woods Hole\n"
            "R_1,3,1,Rte 01\n"
            "R_2,3,2,Tatnuck Square\n"
            "R_18,3,18,Trillium 18\n"
            "R_3,3,3,Vineyard 3\n"
            "R_RT32,3,32,Rt. 32 Orange/Greenfield\n",
        )
        z.writestr(
            "trips.txt",
            "trip_id,route_id,service_id,direction_id,trip_short_name\n"
            "T_LIRR64,R_1,WK,0,64\n",
        )
        z.writestr(
            "stops.txt",
            "stop_id,stop_code,stop_name\nSTOP_BSR,BSR,Bay Shore\n",
        )
    return StaticGtfs(zp)


def _norm(table, rows, gtfs, agency_id="", topo_map=None):
    n = Normalizer.from_table(table)
    return n.normalize(
        rows,
        gtfs,
        feed=table,
        agency_id=agency_id,
        agency_tz=EASTERN,
        topo_map=topo_map,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_mph_to_mps(self):
        assert mph_to_mps(10) == pytest.approx(4.4704)
        assert mph_to_mps(None) is None
        assert mph_to_mps(float("nan")) is None

    def test_kmh_to_mps(self):
        assert kmh_to_mps(18) == pytest.approx(5.0)
        assert kmh_to_mps(None) is None

    def test_naive_local_to_utc(self):
        # 2026-06-16 11:14:40 EDT (UTC-4) == 15:14:40 UTC
        ts = naive_local_to_utc("2026-06-16T11:14:40", EASTERN)
        assert ts == 1781622880
        assert naive_local_to_utc(None, EASTERN) is None
        assert naive_local_to_utc("garbage", EASTERN) is None

    def test_naive_local_to_utc_dst_fallback_uses_poll_time(self):
        # 2026-11-01: clocks fall back at 02:00 EDT -> 01:00 EST, so the wall
        # clock "01:30" maps to two UTC instants an hour apart.
        edt = 1793511000  # 01:30 EDT (-04:00) == 05:30 UTC (earlier instant)
        est = 1793514600  # 01:30 EST (-05:00) == 06:30 UTC (later instant)
        amb = "2026-11-01T01:30:00"
        # No reference -> fold=0, the earlier instant.
        assert naive_local_to_utc(amb, EASTERN) == edt
        # Poll anchored just after the EST occurrence resolves to EST.
        assert naive_local_to_utc(amb, EASTERN, est + 90) == est
        # Poll anchored just after the EDT occurrence resolves to EDT.
        assert naive_local_to_utc(amb, EASTERN, edt + 90) == edt
        # A reference far from an unambiguous time doesn't perturb it.
        assert naive_local_to_utc("2026-06-16T11:14:40", EASTERN, 0) == 1781622880

    def test_iso_offset_to_utc(self):
        assert iso_offset_to_utc("2026-06-16T12:06:50.000-04:00") == 1781626010
        assert iso_offset_to_utc("2026-06-16T15:14:54Z") == 1781622894
        assert iso_offset_to_utc(None) is None

    def test_brta_candidate(self):
        assert brta_candidate_short_name("Wk Rt 01") == "1"
        assert brta_candidate_short_name("Rte 34") == "34"
        assert brta_candidate_short_name("Route 5 Loop") == "5"
        assert brta_candidate_short_name("Rte 21 Express") == "21"
        assert brta_candidate_short_name(None) is None

    def test_combine_clock_with_poll_date(self):
        poll = naive_local_to_utc("2026-06-16T15:45:00", EASTERN)
        ts = combine_clock_with_poll_date("03:41 PM", poll, EASTERN)
        # 03:41 PM on the poll's local date
        assert ts == naive_local_to_utc("2026-06-16T15:41:00", EASTERN)

    def test_combine_clock_rolls_back_near_midnight(self):
        # Poll just after midnight; clock "11:59 PM" belongs to the previous day.
        poll = naive_local_to_utc("2026-06-16T00:05:00", EASTERN)
        ts = combine_clock_with_poll_date("11:59 PM", poll, EASTERN)
        assert ts == naive_local_to_utc("2026-06-15T23:59:00", EASTERN)


# ---------------------------------------------------------------------------
# Schema / registry
# ---------------------------------------------------------------------------


def test_every_row_conforms_to_schema(gtfs):
    # Every schema column must be present on the row (rows also carry an in-memory
    # `feed` key that is dropped against the schema on write).
    row = _norm(
        "lirr_trains",
        [
            {
                "train_num": "64",
                "current_stop_code": "BSR",
                "speed_mph": 51.7,
                "location_timestamp": 1781620580,
            }
        ],
        gtfs,
    )[0]
    schema_names = {f.name for f in NORMALIZED_VEHICLES_SCHEMA}
    assert schema_names <= set(row)
    assert "feed" in row  # carried in memory, not in the stored schema


def test_unknown_table_raises():
    with pytest.raises(KeyError):
        Normalizer.from_table("nope")


def test_only_swiv_declares_topo_dependency():
    # The driver loads a topo sidecar iff the normalizer declares needs_topo;
    # only Swiv (idLigne -> nomCommercial) does.
    needs = {t for t in Normalizer._registry if Normalizer.from_table(t).needs_topo}
    assert needs == {"swiv_vehicles"}


# ---------------------------------------------------------------------------
# Per-normalizer
# ---------------------------------------------------------------------------


def test_lirr(gtfs):
    row = _norm(
        "lirr_trains",
        [
            {
                "train_num": "64",
                "current_stop_code": "BSR",
                "speed_mph": 51.7,
                "location_timestamp": 1781620580,
                "latitude": 40.7,
                "longitude": -73.3,
            }
        ],
        gtfs,
    )[0]
    assert row["trip_id"] == "T_LIRR64"  # train_num -> trip_short_names
    assert row["stop_id"] == "STOP_BSR"  # stop_code -> stop_id
    assert row["speed_mps"] == pytest.approx(51.7 * 0.44704)
    assert row["ts_utc"] == 1781620580  # location_timestamp passthrough


def test_lirr_unknown_codes_stay_none(gtfs):
    row = _norm(
        "lirr_trains",
        [
            {
                "train_num": "9999",
                "current_stop_code": "ZZ",
                "speed_mph": None,
                "location_timestamp": 1,
            }
        ],
        gtfs,
    )[0]
    assert row["trip_id"] is None
    assert row["stop_id"] is None


def test_mwrta_exact_route_unknown_speed(gtfs):
    row = _norm(
        "mwrta_vehicles",
        [
            {
                "vehicle_id": 999,
                "route": "RT04S",
                "speed": 20.4,
                "vehicle_datetime": "2026-06-16T11:14:40",
                "feed_timestamp": 1781622000,
            }
        ],
        gtfs,
        agency_id="MWRTA",
    )[0]
    assert row["route_id"] == "R_RT04S"
    assert row["resolution_status"] == "resolved"
    assert row["speed_mps"] is None  # units unknown -> not fabricated
    assert row["raw_speed"] == 20.4
    assert row["speed_unit"] == "unknown"
    assert row["ts_utc"] == 1781622880  # naive-local -> UTC


def test_ccrta_substring_match(gtfs):
    row = _norm(
        "mwrta_vehicles",
        [
            {
                "vehicle_id": 10,
                "route": "604",
                "route_name": "Sealine",
                "speed": 0.0,
                "vehicle_datetime": "2026-06-16T11:13:55",
                "feed_timestamp": 1,
            }
        ],
        gtfs,
        agency_id="CCRTA",
    )[0]
    assert row["route_id"] == "R_CC"  # "Sealine" substring of long name
    assert row["resolution_status"] == "resolved"


def test_ccrta_fuzzy_match_prefers_tightest(tmp_path):
    # Two long names both contain the token; the shorter (tighter) one wins,
    # deterministically, regardless of routes.txt order.
    zp = tmp_path / "feed.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(
            "routes.txt",
            "route_id,route_type,route_short_name,route_long_name\n"
            "R_LONG,3,L,Hyannis Loop via Barnstable\n"
            "R_SHORT,3,S,Hyannis\n",
        )
    gtfs = StaticGtfs(zp)
    from analysis.normalize import resolve_route_ccrta

    # "Hyannis" is an exact short/long key on R_SHORT -> exact path, R_SHORT.
    assert resolve_route_ccrta("Hyannis", gtfs)[0] == "R_SHORT"
    # A token that only substring-matches both picks the shorter name (R_SHORT).
    assert resolve_route_ccrta("yanni", gtfs) == ("R_SHORT", "resolved")


def test_brta_regex_and_cleared_trip(gtfs):
    row = _norm(
        "routematch_vehicles",
        [
            {
                "vehicle_id": "1959",
                "route_id": "Wk Rt 01",
                "trip_id": "Rte 01 1130 in",
                "speed": 12.0,
                "last_update": "2026-06-16T12:06:50.000-04:00",
            }
        ],
        gtfs,
        agency_id="BRTA",
    )[0]
    assert row["route_id"] == "R_1"  # "Wk Rt 01" -> "1" -> R_1
    assert row["trip_id"] is None  # RouteMatch trip cleared
    assert row["speed_mps"] == pytest.approx(12.0 * 0.44704)
    assert row["ts_utc"] == 1781626010


def test_trillium_passthrough_shortname(gtfs):
    row = _norm(
        "trillium_vehicles",
        [
            {
                "vehicle_id": 8841,
                "route_short_name": "18",
                "route_id": "10730",
                "speed": 9,
                "heading_degrees": 151.5,
                "last_updated": "2026-06-16T15:14:54Z",
            }
        ],
        gtfs,
    )[0]
    assert row["route_id"] == "R_18"
    assert row["bearing"] == 151.5
    assert row["raw_speed"] == 9.0
    assert row["speed_unit"] == "unknown"  # Trillium speed not converted


def test_swiv_topo_resolution(gtfs):
    row = _norm(
        "swiv_vehicles",
        [
            {
                "vehicle_id": 1017,
                "route_line_id": 16103,
                "speed": 18,
                "heading": 191,
                "feed_timestamp": 1781622000,
            }
        ],
        gtfs,
        agency_id="GATRA",
        topo_map={"16103": "1"},
    )[0]
    assert row["route_id"] == "R_1"  # idLigne -> nomCommercial "1" -> R_1
    assert row["speed_mps"] == pytest.approx(5.0)  # km/h -> m/s
    assert row["ts_utc"] == 1781622000  # feed_timestamp (no per-vehicle ts)


def test_swiv_unresolved_without_topo(gtfs):
    row = _norm(
        "swiv_vehicles",
        [{"vehicle_id": 1, "route_line_id": 99999, "speed": 0, "feed_timestamp": 1}],
        gtfs,
        agency_id="GATRA",
    )[0]
    assert row["route_id"] is None
    assert row["resolution_status"] == "route_unresolved"


def test_swiv_wrta_static_map(gtfs):
    # WRTA resolves via its hardcoded idLigne->short-name map even without topo.
    row = _norm(
        "swiv_vehicles",
        [{"vehicle_id": 6010, "route_line_id": 18046, "speed": 8, "feed_timestamp": 1}],
        gtfs,
        agency_id="WRTA",
    )[0]
    assert row["route_id"] == "R_2"  # 18046 -> "2" -> R_2


def test_vta(gtfs):
    row = _norm(
        "vta_vehicles",
        [
            {
                "vehicle_id": 22,
                "headsign_text": "3",
                "velocity": 18,
                "bearing": 279,
                "last_update": "2026-03-18T15:46:58",
                "feed_timestamp": 1,
            }
        ],
        gtfs,
        agency_id="VINEYARD_VTA",
    )[0]
    assert row["route_id"] == "R_3"
    assert row["speed_mps"] == pytest.approx(18 * 0.44704)


def test_passio(gtfs):
    row = _norm(
        "passio_vehicles",
        [
            {
                "vehicle_id": 11253,
                "route_name": "Rt. 32 Orange/Greenfield",
                "route_id": "30848",
                "speed": None,
                "calculated_course": 93.9,
                "created": "03:41 PM",
                "feed_timestamp": 1781638860,
            }
        ],
        gtfs,
        agency_id="FRTA",
    )[0]
    assert row["route_id"] == "R_RT32"  # exact display-name match
    assert row["trip_id"] is None
    assert row["speed_unit"] == "unknown"
    assert row["bearing"] == 93.9


def test_unresolved_route_status(gtfs):
    row = _norm(
        "vta_vehicles",
        [
            {
                "vehicle_id": 1,
                "headsign_text": "ZZ",
                "velocity": 0,
                "bearing": 0,
                "last_update": "2026-03-18T15:46:58",
                "feed_timestamp": 1,
            }
        ],
        gtfs,
        agency_id="VINEYARD_VTA",
    )[0]
    assert row["route_id"] is None
    assert row["resolution_status"] == "route_unresolved"
