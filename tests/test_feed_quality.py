import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import fs as pafs

# scripts/ is not a package; load the module by path. Register it in sys.modules
# before exec so @dataclass can resolve the module for its string annotations.
_SPEC = importlib.util.spec_from_file_location(
    "feed_quality",
    Path(__file__).resolve().parent.parent / "scripts" / "feed_quality.py",
)
feed_quality = importlib.util.module_from_spec(_SPEC)
sys.modules["feed_quality"] = feed_quality
_SPEC.loader.exec_module(feed_quality)

FeedReport = feed_quality.FeedReport
DAY = dt.date(2026, 5, 1)


class TestCountSet:
    def test_string_excludes_null_and_empty(self):
        col = pa.chunked_array([pa.array(["a", "", None, "b"])])
        assert feed_quality._count_set(col) == 2

    def test_int_counts_non_null_including_zero(self):
        col = pa.chunked_array([pa.array([0, 1, None], type=pa.int8())])
        assert feed_quality._count_set(col) == 2  # 0 is a real direction


class TestResolve:
    def test_prefers_dotted_then_flat(self):
        assert feed_quality._resolve("route", {"vehicle.trip.route_id"}) == (
            "vehicle.trip.route_id"
        )
        assert feed_quality._resolve("route", {"route_id"}) == "route_id"
        assert feed_quality._resolve("route", {"other"}) is None


class TestNotes:
    def _report(self, **set_pct) -> FeedReport:
        rows = 100
        counts = {
            f: int(set_pct.get(f, 100)) for f in ("route", "dir", "stop", "status")
        }
        return FeedReport("f", 1, rows, counts, "flat")

    def test_no_stop_suggests_trip_updates(self):
        assert "trip-updates" in self._report(stop=0, status=0).notes

    def test_no_status_suggests_position_based(self):
        assert "position-based" in self._report(status=0).notes

    def test_no_direction_suggests_gtfs(self):
        assert "GTFS" in self._report(dir=0).notes

    def test_no_route(self):
        assert "no route_id" in self._report(route=0).notes

    def test_clean_feed_has_no_notes(self):
        assert self._report().notes == ""

    def test_zero_rows(self):
        assert (
            FeedReport(
                "f", 0, 0, dict.fromkeys(("route", "dir", "stop", "status"), 0), "—"
            ).notes
            == "no rows"
        )


def _write_vehicles(path: Path, columns: dict[str, pa.Array]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), path / "data.parquet")


def _local_store(root: Path) -> "feed_quality.CuratedStore":
    return feed_quality.CuratedStore(
        pafs.LocalFileSystem(), str(root), f"{root}/vehicles"
    )


class TestScanFeed:
    def test_mixed_schemes_aggregate(self, tmp_path):
        base = tmp_path / "vehicles" / "feed=demo"
        # Day 1: dotted scheme, stop set on both rows.
        _write_vehicles(
            base / "year=2026/month=5/day=1",
            {
                "vehicle.trip.route_id": pa.array(["R", "R"]),
                "vehicle.trip.direction_id": pa.array([0, 1], type=pa.int8()),
                "vehicle.stop_id": pa.array(["S1", "S2"]),
                "vehicle.current_status": pa.array(["STOPPED_AT", "STOPPED_AT"]),
            },
        )
        # Day 2: flat scheme, stop missing on both rows (null).
        _write_vehicles(
            base / "year=2026/month=5/day=2",
            {
                "route_id": pa.array(["R", "R"]),
                "direction_id": pa.array([None, None], type=pa.int8()),
                "stop_id": pa.array([None, None], type=pa.string()),
                "current_status": pa.array([None, None], type=pa.string()),
            },
        )

        rpt = feed_quality.scan_feed("demo", _local_store(tmp_path), day=None)
        assert rpt.rows == 4
        assert rpt.days == 2
        assert rpt.scheme == "mixed"
        assert rpt.pct("route") == 100.0  # all 4 rows
        assert rpt.pct("stop") == 50.0  # only day 1
        assert rpt.pct("dir") == 50.0  # day 1's 0 and 1 count; day 2 null
        # 50% coverage clears the <1% flags, so no notes.
        assert rpt.notes == ""

    def test_day_filter(self, tmp_path):
        base = tmp_path / "vehicles" / "feed=demo"
        _write_vehicles(
            base / "year=2026/month=5/day=1",
            {"route_id": pa.array(["R"]), "stop_id": pa.array(["S1"])},
        )
        _write_vehicles(
            base / "year=2026/month=5/day=2",
            {"route_id": pa.array(["R", "R"]), "stop_id": pa.array(["S1", "S2"])},
        )
        store = _local_store(tmp_path)
        assert store.feeds() == ["demo"]
        rpt = feed_quality.scan_feed("demo", store, day=DAY)
        assert rpt.rows == 1
        assert rpt.days == 1
