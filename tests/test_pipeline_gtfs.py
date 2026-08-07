"""Tests for pipeline/gtfs.py — the static-GTFS normalization marts.

HTTP is monkeypatched (analysis.gtfs_fetcher.requests.get), same style as
tests/test_analysis_gtfs_fetcher.py. No network calls.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from analysis.gtfs_fetcher import GtfsResolver
from analysis.static_gtfs import StaticGtfs
from pipeline import gtfs as pgtfs

SAMPLE_CATALOG = (
    "feed_start_date,feed_end_date,feed_version,archive_url,archive_note\n"
    "20260520,20260907,2026-05-21T00:57:40.772601Z,https://example/v3.zip,\n"
)

SAMPLE_STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon\nS1,001,Union Station,38.9,-77.0\n"
)
SAMPLE_CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
    "start_date,end_date\nWD,1,1,1,1,1,0,0,20260501,20260531\n"
)
SAMPLE_CALENDAR_DATES = "service_id,date,exception_type\nWD,20260525,2\n"
SAMPLE_SHAPES = (
    "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
    "SH1,38.90,-77.00,1\nSH1,38.91,-77.01,2\n"
)
SAMPLE_ROUTE_PATTERNS = (
    "route_pattern_id,route_id,direction_id,route_pattern_name,"
    "route_pattern_typicality,representative_trip_id\n"
    "R-1-0,R,0,Harvard - Nubian,1,T1\n"
)
SAMPLE_DIRECTIONS = (
    "route_id,direction_id,direction,direction_destination\nR,0,Outbound,Nubian\n"
)
SAMPLE_CHECKPOINTS = "checkpoint_id,checkpoint_name\nHARSQ,Harvard Square\n"

CONFIG_YAML = """
writer:
  landing_dir: ./landing
  curated_dir: ./curated
agencies:
  - agency_id: WMATA
    name: WMATA
    region: DC
    timezone: America/New_York
    base_url: https://example.com
    mdb_feed_id: mdb-1847
    auth:
      type: none
    feeds:
      - name: wmata-vehicles
        path: /vehicles
  - agency_id: NOMDB
    name: No MDB Agency
    region: Nowhere
    timezone: America/New_York
    base_url: https://example.com
    auth:
      type: none
    feeds:
      - name: nomdb-vehicles
        path: /vehicles
"""


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    content: bytes = b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _build_zip_bytes(
    stops: str | None = None,
    calendar: str | None = None,
    calendar_dates: str | None = None,
    shapes: str | None = None,
    route_patterns: str | None = None,
    directions: str | None = None,
    checkpoints: str | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if stops is not None:
            z.writestr("stops.txt", stops)
        if calendar is not None:
            z.writestr("calendar.txt", calendar)
        if calendar_dates is not None:
            z.writestr("calendar_dates.txt", calendar_dates)
        if shapes is not None:
            z.writestr("shapes.txt", shapes)
        if route_patterns is not None:
            z.writestr("route_patterns.txt", route_patterns)
        if directions is not None:
            z.writestr("directions.txt", directions)
        if checkpoints is not None:
            z.writestr("checkpoints.txt", checkpoints)
    return buf.getvalue()


def _patch_gtfs_http(monkeypatch, zip_bytes: bytes, catalog_text: str = SAMPLE_CATALOG):
    call_counts = {"catalog": 0, "download": 0}

    def fake_get(url, params=None, stream=False, timeout=None):
        if "archived_feeds.txt" in url:
            call_counts["catalog"] += 1
            return FakeResponse(text=catalog_text)
        call_counts["download"] += 1
        return FakeResponse(content=zip_bytes)

    monkeypatch.setattr("analysis.gtfs_fetcher.requests.get", fake_get)
    return call_counts


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "feeds.yaml"
    p.write_text(CONFIG_YAML)
    return p


class TestRowBuilders:
    """Pure row-builder functions given a StaticGtfs — no resolver/CLI plumbing."""

    def _gtfs(self, tmp_path: Path) -> StaticGtfs:
        zp = tmp_path / "feed.zip"
        zp.write_bytes(
            _build_zip_bytes(
                stops=SAMPLE_STOPS,
                calendar=SAMPLE_CALENDAR,
                calendar_dates=SAMPLE_CALENDAR_DATES,
                shapes=SAMPLE_SHAPES,
                route_patterns=SAMPLE_ROUTE_PATTERNS,
                directions=SAMPLE_DIRECTIONS,
                checkpoints=SAMPLE_CHECKPOINTS,
            )
        )
        return StaticGtfs(zp)

    def test_stops_rows(self, tmp_path):
        rows = pgtfs._stops_rows(self._gtfs(tmp_path), "v1")
        assert rows == [
            {
                "stop_id": "S1",
                "stop_code": "001",
                "stop_name": "Union Station",
                "stop_lat": 38.9,
                "stop_lon": -77.0,
                "version_slug": "v1",
            }
        ]

    def test_calendar_rows(self, tmp_path):
        rows = pgtfs._calendar_rows(self._gtfs(tmp_path), "v1")
        assert len(rows) == 1
        row = rows[0]
        assert row["service_id"] == "WD"
        assert row["monday"] is True
        assert row["saturday"] is False
        assert row["start_date"] == "2026-05-01"
        assert row["end_date"] == "2026-05-31"
        assert row["version_slug"] == "v1"

    def test_calendar_dates_rows(self, tmp_path):
        rows = pgtfs._calendar_dates_rows(self._gtfs(tmp_path), "v1")
        assert rows == [
            {
                "service_id": "WD",
                "date": "2026-05-25",
                "exception_type": 2,
                "version_slug": "v1",
            }
        ]

    def test_shapes_rows(self, tmp_path):
        rows = pgtfs._shapes_rows(self._gtfs(tmp_path), "v1")
        assert [r["shape_pt_sequence"] for r in rows] == [1, 2]
        assert rows[0]["shape_id"] == "SH1"
        assert rows[0]["shape_dist_traveled"] is None

    def test_route_patterns_rows(self, tmp_path):
        rows = pgtfs._route_patterns_rows(self._gtfs(tmp_path), "v1")
        assert rows == [
            {
                "route_pattern_id": "R-1-0",
                "route_id": "R",
                "direction_id": 0,
                "route_pattern_name": "Harvard - Nubian",
                "route_pattern_typicality": 1,
                "representative_trip_id": "T1",
                "version_slug": "v1",
            }
        ]

    def test_directions_rows(self, tmp_path):
        rows = pgtfs._directions_rows(self._gtfs(tmp_path), "v1")
        assert rows == [
            {
                "route_id": "R",
                "direction_id": 0,
                "direction": "Outbound",
                "direction_destination": "Nubian",
                "version_slug": "v1",
            }
        ]

    def test_checkpoints_rows(self, tmp_path):
        rows = pgtfs._checkpoints_rows(self._gtfs(tmp_path), "v1")
        assert rows == [
            {
                "checkpoint_id": "HARSQ",
                "checkpoint_name": "Harvard Square",
                "version_slug": "v1",
            }
        ]

    def test_empty_when_optional_files_absent(self, tmp_path):
        zp = tmp_path / "feed.zip"
        zp.write_bytes(_build_zip_bytes())  # no tables at all
        gtfs = StaticGtfs(zp)
        assert pgtfs._stops_rows(gtfs, "v1") == []
        assert pgtfs._calendar_rows(gtfs, "v1") == []
        assert pgtfs._calendar_dates_rows(gtfs, "v1") == []
        assert pgtfs._shapes_rows(gtfs, "v1") == []
        assert pgtfs._route_patterns_rows(gtfs, "v1") == []
        assert pgtfs._directions_rows(gtfs, "v1") == []
        assert pgtfs._checkpoints_rows(gtfs, "v1") == []


class TestProcessFeedDay:
    def test_builds_version_marts_and_manifest(self, tmp_path, monkeypatch):
        zip_bytes = _build_zip_bytes(
            stops=SAMPLE_STOPS,
            calendar=SAMPLE_CALENDAR,
            shapes=SAMPLE_SHAPES,
            route_patterns=SAMPLE_ROUTE_PATTERNS,
            directions=SAMPLE_DIRECTIONS,
            checkpoints=SAMPLE_CHECKPOINTS,
        )
        _patch_gtfs_http(monkeypatch, zip_bytes)
        resolver = GtfsResolver("mdb-1847", "wmata", cache_dir=tmp_path / "cache")
        curated = tmp_path / "curated"
        written: set[tuple[str, str]] = set()

        result = pgtfs.process_feed_day(
            "wmata-vehicles", dt.date(2026, 5, 20), resolver, curated, False, written
        )
        assert result == {
            "gtfs_stops": 1,
            "gtfs_calendar": 1,
            "gtfs_calendar_dates": 0,
            "gtfs_shapes": 2,
            "gtfs_route_patterns": 1,
            "gtfs_directions": 1,
            "gtfs_checkpoints": 1,
            "route_shapes": 0,
            "route_shape_stops": 0,
            "manifest": 1,
        }
        assert len(written) == 1
        version_slug = next(iter(written))[1]
        assert version_slug == "20260521T005740"

        stops_path = pgtfs._version_partition_path(
            curated, "gtfs_stops", "wmata-vehicles", version_slug
        )
        calendar_dates_path = pgtfs._version_partition_path(
            curated, "gtfs_calendar_dates", "wmata-vehicles", version_slug
        )
        manifest_path = pgtfs._mart_path(
            curated, "gtfs_versions", "wmata-vehicles", dt.date(2026, 5, 20)
        )
        assert stops_path.exists()
        # Written even though empty, so the idempotency check below doesn't
        # re-fetch forever for a feed with no calendar_dates.txt.
        assert calendar_dates_path.exists()
        assert manifest_path.exists()

    def test_second_day_same_version_skips_rebuild(self, tmp_path, monkeypatch):
        zip_bytes = _build_zip_bytes(stops=SAMPLE_STOPS)
        call_counts = _patch_gtfs_http(monkeypatch, zip_bytes)
        resolver = GtfsResolver("mdb-1847", "wmata", cache_dir=tmp_path / "cache")
        curated = tmp_path / "curated"
        written: set[tuple[str, str]] = set()

        pgtfs.process_feed_day(
            "wmata-vehicles", dt.date(2026, 5, 20), resolver, curated, False, written
        )
        assert call_counts["download"] == 1

        result2 = pgtfs.process_feed_day(
            "wmata-vehicles", dt.date(2026, 5, 21), resolver, curated, False, written
        )
        assert call_counts["download"] == 1  # no second parse/download
        assert result2["gtfs_stops"] == 0  # version marts not rewritten
        assert result2["manifest"] == 1  # but a new day still gets its own row

        manifest_path_1 = pgtfs._mart_path(
            curated, "gtfs_versions", "wmata-vehicles", dt.date(2026, 5, 20)
        )
        manifest_path_2 = pgtfs._mart_path(
            curated, "gtfs_versions", "wmata-vehicles", dt.date(2026, 5, 21)
        )
        assert manifest_path_1.exists()
        assert manifest_path_2.exists()

    def test_force_rebuilds_version_marts(self, tmp_path, monkeypatch):
        zip_bytes = _build_zip_bytes(stops=SAMPLE_STOPS)
        _patch_gtfs_http(monkeypatch, zip_bytes)
        resolver = GtfsResolver("mdb-1847", "wmata", cache_dir=tmp_path / "cache")
        curated = tmp_path / "curated"
        pgtfs.process_feed_day(
            "wmata-vehicles", dt.date(2026, 5, 20), resolver, curated, False, set()
        )

        # A fresh run (new written_versions set) with existing files on disk
        # and --force set must rebuild rather than skip.
        result = pgtfs.process_feed_day(
            "wmata-vehicles", dt.date(2026, 5, 20), resolver, curated, True, set()
        )
        assert result["gtfs_stops"] == 1

    def test_no_snapshot_covers_day_returns_none(self, tmp_path, monkeypatch):
        zip_bytes = _build_zip_bytes(stops=SAMPLE_STOPS)
        _patch_gtfs_http(monkeypatch, zip_bytes)
        resolver = GtfsResolver("mdb-1847", "wmata", cache_dir=tmp_path / "cache")
        curated = tmp_path / "curated"

        result = pgtfs.process_feed_day(
            "wmata-vehicles", dt.date(2020, 1, 1), resolver, curated, False, set()
        )
        assert result is None


class TestMain:
    def test_no_mdb_feed_id_clean_skip(self, tmp_path, config_path, capsys):
        rc = pgtfs.main(
            [
                "--feed",
                "nomdb-vehicles",
                "--day",
                "2026-05-20",
                "-c",
                str(config_path),
                "--curated-dir",
                str(tmp_path / "curated"),
            ]
        )
        assert rc == 0
        assert "no mdb_feed_id" in capsys.readouterr().err

    def test_catalog_failure_skips_agency_not_whole_run(
        self, tmp_path, config_path, monkeypatch, capsys
    ):
        def fake_get(url, params=None, stream=False, timeout=None):
            raise requests.exceptions.ConnectionError("boom")

        monkeypatch.setattr("analysis.gtfs_fetcher.requests.get", fake_get)

        rc = pgtfs.main(
            [
                "--feed",
                "wmata-vehicles",
                "--day",
                "2026-05-20",
                "-c",
                str(config_path),
                "--curated-dir",
                str(tmp_path / "curated"),
                "--gtfs-cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        assert rc == 0  # a bad agency must not abort the run / break `&&` chains
        assert "GTFS catalog/zip unavailable" in capsys.readouterr().err

    def test_end_to_end_writes_marts(self, tmp_path, config_path, monkeypatch):
        zip_bytes = _build_zip_bytes(stops=SAMPLE_STOPS, calendar=SAMPLE_CALENDAR)
        _patch_gtfs_http(monkeypatch, zip_bytes)
        curated = tmp_path / "curated"

        rc = pgtfs.main(
            [
                "--feed",
                "wmata-vehicles",
                "--day",
                "2026-05-20",
                "-c",
                str(config_path),
                "--curated-dir",
                str(curated),
                "--gtfs-cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        assert rc == 0
        manifest_path = pgtfs._mart_path(
            curated, "gtfs_versions", "wmata-vehicles", dt.date(2026, 5, 20)
        )
        stops_path = pgtfs._version_partition_path(
            curated, "gtfs_stops", "wmata-vehicles", "20260521T005740"
        )
        assert manifest_path.exists()
        assert stops_path.exists()
