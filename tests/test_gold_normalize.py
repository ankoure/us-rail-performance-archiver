"""End-to-end test for gold.normalize_one (raw curated -> normalized_vehicles)."""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

import gold
from analysis.normalize import NORMALIZED_VEHICLES_SCHEMA
from analysis.static_gtfs import StaticGtfs
from gold import normalize_one, _raw_table_path, _normalized_path

EASTERN = ZoneInfo("America/New_York")
DAY = dt.date(2026, 6, 16)


def _gtfs(tmp_path: Path) -> StaticGtfs:
    zp = tmp_path / "feed.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(
            "routes.txt",
            "route_id,route_type,route_short_name,route_long_name\n"
            "R_RT04S,3,RT04S,MetroWest 4\n",
        )
    return StaticGtfs(zp)


def _write_raw(curated: Path, table: str, feed: str, rows: list[dict]) -> None:
    path = _raw_table_path(curated, table, feed, DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_normalize_one_writes_normalized_table(tmp_path):
    curated = tmp_path / "curated"
    _write_raw(
        curated,
        "mwrta_vehicles",
        "mwrta-vehicles",
        [
            {
                "vehicle_id": "999",
                "route": "RT04S",
                "route_name": None,
                "speed": 20.4,
                "heading": 109.0,
                "vehicle_datetime": "2026-06-16T11:14:40",
                "latitude": 42.27,
                "longitude": -71.41,
                "feed_timestamp": 1781622000,
            },
        ],
    )
    gtfs = _gtfs(tmp_path)

    n = normalize_one(
        "mwrta-vehicles",
        DAY,
        "MWRTA",
        EASTERN,
        "mwrta_vehicles",
        curated,
        gtfs_for=lambda f, d: gtfs,
        topo_for=lambda a, d: None,
        force=False,
    )
    assert n == 1

    out = _normalized_path(curated, "mwrta-vehicles", DAY)
    assert out.exists()
    # The stored file holds exactly the canonical columns (no `feed` — that's the
    # partition key; read_schema reads file metadata without partition inference).
    assert pq.read_schema(out).names == NORMALIZED_VEHICLES_SCHEMA.names
    row = pq.read_table(out).to_pylist()[0]
    assert row["route_id"] == "R_RT04S"
    assert row["resolution_status"] == "resolved"
    assert row["raw_speed"] == 20.4
    assert row["speed_unit"] == "unknown"
    assert row["ts_utc"] == 1781622880  # naive-local -> UTC


def test_normalize_one_skips_missing_partition(tmp_path):
    curated = tmp_path / "curated"  # nothing written
    gtfs = _gtfs(tmp_path)
    n = normalize_one(
        "mwrta-vehicles",
        DAY,
        "MWRTA",
        EASTERN,
        "mwrta_vehicles",
        curated,
        gtfs_for=lambda f, d: gtfs,
        topo_for=lambda a, d: None,
        force=False,
    )
    assert n is None
    assert not _normalized_path(curated, "mwrta-vehicles", DAY).exists()


def test_normalize_one_skips_when_no_gtfs(tmp_path):
    curated = tmp_path / "curated"
    _write_raw(
        curated,
        "mwrta_vehicles",
        "mwrta-vehicles",
        [{"vehicle_id": "1", "route": "RT04S", "feed_timestamp": 1}],
    )
    n = normalize_one(
        "mwrta-vehicles",
        DAY,
        "MWRTA",
        EASTERN,
        "mwrta_vehicles",
        curated,
        gtfs_for=lambda f, d: None,
        topo_for=lambda a, d: None,
        force=False,
    )
    assert n is None
    assert not _normalized_path(curated, "mwrta-vehicles", DAY).exists()


def test_normalize_one_swiv_uses_topo(tmp_path):
    curated = tmp_path / "curated"
    _write_raw(
        curated,
        "swiv_vehicles",
        "gatra-vehicles",
        [
            {
                "vehicle_id": "1017",
                "route_line_id": 16103,
                "speed": 18,
                "heading": 191,
                "latitude": 41.9,
                "longitude": -70.7,
                "feed_timestamp": 1781622000,
            }
        ],
    )
    # GTFS that knows route "1"; topo maps idLigne 16103 -> "1".
    zp = tmp_path / "swiv.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(
            "routes.txt",
            "route_id,route_type,route_short_name,route_long_name\nR_1,3,1,Route 1\n",
        )
    gtfs = StaticGtfs(zp)

    n = normalize_one(
        "gatra-vehicles",
        DAY,
        "GATRA",
        EASTERN,
        "swiv_vehicles",
        curated,
        gtfs_for=lambda f, d: gtfs,
        topo_for=lambda a, d: {"16103": "1"},
        force=False,
    )
    assert n == 1
    row = pq.read_table(_normalized_path(curated, "gatra-vehicles", DAY)).to_pylist()[0]
    assert row["route_id"] == "R_1"
    assert row["speed_mps"] is not None  # km/h -> m/s


def test_make_topo_resolver_reads_config_derived_feed(tmp_path, monkeypatch):
    # The topo feed name is resolved from config, not hardcoded. Stub the lookup
    # to a feed name and land a swiv_ligne partition under it.
    monkeypatch.setattr(
        gold, "load_swiv_topo_feed_map", lambda cp: {"GATRA": "gatra-topo"}
    )
    curated = tmp_path / "curated"
    path = _raw_table_path(curated, "swiv_ligne", "gatra-topo", DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"id_ligne": 16103, "nom_commercial": "1"},
                {"id_ligne": 16104, "nom_commercial": None},  # dropped: no name
                {"id_ligne": None, "nom_commercial": "X"},  # dropped: no id
            ]
        ),
        path,
    )
    topo_for = gold._make_topo_resolver(curated, tmp_path / "cfg.yaml")
    assert topo_for("GATRA", DAY) == {"16103": "1"}  # id stringified, nulls dropped
    # An agency with no topo feed configured returns None.
    assert topo_for("LRTA", DAY) is None


def test_make_topo_resolver_warns_once_when_absent(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(gold, "load_swiv_topo_feed_map", lambda cp: {})
    topo_for = gold._make_topo_resolver(tmp_path / "curated", tmp_path / "cfg.yaml")
    assert topo_for("GATRA", DAY) is None
    assert topo_for("GATRA", DAY) is None  # cached: no duplicate warning
    err = capsys.readouterr().err
    assert err.count("Swiv topo table unavailable") == 1
    assert "GATRA" in err
