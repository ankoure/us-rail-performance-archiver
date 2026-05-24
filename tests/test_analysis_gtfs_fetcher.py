"""Tests for analysis/gtfs_fetcher.py — HTTP is monkeypatched, no network."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from analysis.gtfs_fetcher import (
    GtfsResolver,
    Snapshot,
    ensure_local_zip,
    fetch_catalog,
    pick_snapshot,
)

SAMPLE_CATALOG = (
    "feed_start_date,feed_end_date,feed_version,archive_url,archive_note\n"
    "20260520,20260907,2026-05-21T00:57:40.772601Z,https://example/v3.zip,\n"
    "20260519,20260907,2026-05-20T01:19:20.442659Z,https://example/v2.zip,\n"
    "20260514,20260907,2026-05-15T01:15:18.486701Z,https://example/v1.zip,\n"
)


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    content: bytes = b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        # Yield content in chunks
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestPickSnapshot:
    @pytest.fixture
    def catalog(self) -> pd.DataFrame:
        return pd.read_csv(
            io.StringIO(SAMPLE_CATALOG),
            parse_dates=["feed_start_date", "feed_end_date"],
            date_format="%Y%m%d",
        )

    def test_picks_latest_start_date_within_window(self, catalog):
        # 2026-05-20 is in all three windows; latest start is 05-20 (v3)
        snap = pick_snapshot(catalog, dt.date(2026, 5, 20))
        assert snap.archive_url == "https://example/v3.zip"

    def test_picks_earlier_snapshot_when_latest_doesnt_cover(self, catalog):
        # 2026-05-15 only matches v1 (start=05-14) — v2 starts 05-19, v3 starts 05-20
        snap = pick_snapshot(catalog, dt.date(2026, 5, 15))
        assert snap.archive_url == "https://example/v1.zip"

    def test_raises_when_no_snapshot_covers_date(self, catalog):
        with pytest.raises(LookupError):
            pick_snapshot(catalog, dt.date(2026, 5, 1))

    def test_version_slug_is_filesystem_safe(self, catalog):
        snap = pick_snapshot(catalog, dt.date(2026, 5, 20))
        # 2026-05-21T00:57:40.772601Z → 20260521T005740
        assert snap.version_slug == "20260521T005740"


class TestFetchCatalog:
    def test_parses_csv_response(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse(text=SAMPLE_CATALOG)

        monkeypatch.setattr("analysis.gtfs_fetcher.requests.get", fake_get)
        df = fetch_catalog("mdb-1847", api_url="https://example/api")

        assert captured["url"] == "https://example/api/archived_feeds.txt"
        assert captured["params"] == {"feed_id": "mdb-1847"}
        assert len(df) == 3
        assert df["feed_start_date"].iloc[0] == pd.Timestamp("2026-05-20")

    def test_drops_rows_missing_critical_fields(self, monkeypatch):
        bad_csv = SAMPLE_CATALOG + ",,,,\n"
        monkeypatch.setattr(
            "analysis.gtfs_fetcher.requests.get",
            lambda *a, **k: FakeResponse(text=bad_csv),
        )
        df = fetch_catalog("mdb-1847")
        assert len(df) == 3  # the empty row dropped


class TestEnsureLocalZip:
    @pytest.fixture
    def snapshot(self) -> Snapshot:
        return Snapshot(
            feed_start_date=dt.date(2026, 5, 20),
            feed_end_date=dt.date(2026, 9, 7),
            feed_version="2026-05-21T00:57:40Z",
            archive_url="https://example/v3.zip",
        )

    @pytest.fixture
    def fake_zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("trips.txt", "trip_id\nT1\n")
        return buf.getvalue()

    def test_downloads_when_missing(self, monkeypatch, tmp_path, snapshot, fake_zip_bytes):
        def fake_get(url, stream=False, timeout=None):
            assert url == snapshot.archive_url
            return FakeResponse(content=fake_zip_bytes)

        monkeypatch.setattr("analysis.gtfs_fetcher.requests.get", fake_get)
        path = ensure_local_zip(snapshot, "wmata", cache_dir=tmp_path)

        assert path == tmp_path / "wmata" / "v20260521T005740" / "feed.zip"
        assert path.exists()
        assert path.read_bytes() == fake_zip_bytes

    def test_skips_when_cached(self, monkeypatch, tmp_path, snapshot, fake_zip_bytes):
        # Pre-populate the cache
        dest = tmp_path / "wmata" / "v20260521T005740" / "feed.zip"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(fake_zip_bytes)

        call_count = {"n": 0}

        def fake_get(*a, **k):
            call_count["n"] += 1
            return FakeResponse(content=b"shouldnt happen")

        monkeypatch.setattr("analysis.gtfs_fetcher.requests.get", fake_get)
        path = ensure_local_zip(snapshot, "wmata", cache_dir=tmp_path)
        assert path == dest
        assert call_count["n"] == 0


class TestGtfsResolver:
    def test_loads_each_snapshot_once_across_dates(self, monkeypatch, tmp_path):
        # Build a tiny but valid GTFS zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                "WD,1,1,1,1,1,0,0,20260501,20260531\n",
            )
            z.writestr("trips.txt", "trip_id,route_id,service_id,direction_id\n")
            z.writestr(
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n",
            )
        zip_bytes = buf.getvalue()

        call_counts = {"catalog": 0, "download": 0}

        def fake_get(url, params=None, stream=False, timeout=None):
            if "archived_feeds.txt" in url:
                call_counts["catalog"] += 1
                return FakeResponse(text=SAMPLE_CATALOG)
            else:
                call_counts["download"] += 1
                return FakeResponse(content=zip_bytes)

        monkeypatch.setattr("analysis.gtfs_fetcher.requests.get", fake_get)
        resolver = GtfsResolver("mdb-1847", "wmata", cache_dir=tmp_path)

        # Two dates that share the same snapshot (both fall in v3's window with latest start 05-20)
        g1 = resolver.for_date(dt.date(2026, 5, 20))
        g2 = resolver.for_date(dt.date(2026, 5, 25))

        assert g1 is g2  # same object — memoized
        assert call_counts["catalog"] == 1  # catalog fetched once
        assert call_counts["download"] == 1  # zip downloaded once
