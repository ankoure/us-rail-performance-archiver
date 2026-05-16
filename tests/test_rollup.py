from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterator
from archiver.feed import Feed
import pyarrow.parquet as pq
import pyarrow as pa
from archiver.decoder import VehicleRow
from archiver.rollup import _schema_from_dataclass, Rollup
import pytest
from archiver.decoder import Row, Decoder, TableSpec
from archiver.parser import Parser


class FakeParser(Parser):
    def parse(self, body: bytes):
        return b""  # FakeDecoder ignores its input, so this can be anything


@dataclass
class FakeRow(Row):
    feed_timestamp: int
    destination: str
    direction: str


class FakeDecoder(Decoder):
    produces = {FakeRow: TableSpec("fakes")}

    def decode(self, raw: bytes, *, fetched_at: int | None = None) -> Iterator[Row]:

        for _ in range(5):
            feed_timestamp = datetime.now().timestamp()
            destination = "Hollywood Hills"
            direction = "West"
            yield (
                FakeRow(
                    feed_timestamp=feed_timestamp,
                    destination=destination,
                    direction=direction,
                )
            )


# tests/test_rollup.py
def test_schema_from_dataclass_basic_types():
    schema = _schema_from_dataclass(VehicleRow)
    field_types = {f.name: f.type for f in schema}
    assert field_types["vehicle_id"] == pa.string()
    assert field_types["latitude"] == pa.float64()
    assert field_types["direction_id"] == pa.int64()
    # all fields nullable
    assert all(f.nullable for f in schema)


def test_streaming_writer_empty_writes_nothing(tmp_path):
    out = tmp_path / "data.parquet"
    schema = _schema_from_dataclass(VehicleRow)
    with Rollup._streaming_writer(out, schema):
        pass
    assert not out.exists()
    assert not out.with_suffix(".parquet.tmp").exists()


def test_streaming_writer_one_batch(tmp_path):
    out = tmp_path / "data.parquet"
    schema = _schema_from_dataclass(VehicleRow)
    with Rollup._streaming_writer(out, schema) as append:
        append(asdict(VehicleRow(vehicle_id="v1")))
        append(asdict(VehicleRow(vehicle_id="v2")))

    table = pq.ParquetFile(out).read()
    assert table.num_rows == 2
    assert table.column("vehicle_id").to_pylist() == ["v1", "v2"]


def test_streaming_writer_multiple_batches(tmp_path):
    out = tmp_path / "data.parquet"
    schema = _schema_from_dataclass(VehicleRow)
    with Rollup._streaming_writer(out, schema, batch_size=3) as append:
        for i in range(7):  # forces 2 flushes + final
            append(asdict(VehicleRow(vehicle_id=f"v{i}")))

    table = pq.ParquetFile(out).read()
    assert table.num_rows == 7


def test_streaming_writer_exception_no_orphan(tmp_path):
    out = tmp_path / "data.parquet"
    schema = _schema_from_dataclass(VehicleRow)
    with pytest.raises(RuntimeError):
        with Rollup._streaming_writer(out, schema) as append:
            append(asdict(VehicleRow(vehicle_id="v1")))
            raise RuntimeError("boom")

    # final parquet shouldn't exist (atomic rename never happened)
    assert not out.exists()
    # tmp shouldn't be left behind either
    assert not out.with_suffix(".parquet.tmp").exists()


def test_rollup_routes_unknwon_decoder_to_its_own_table(tmp_path):
    pass


def test_skip_happens_when_outputs_exist(tmp_path, monkeypatch):
    landing_dir = tmp_path / "landing"
    curated_dir = tmp_path / "curated"
    feed = Feed(
        name="fake-feed",
        path="/whatever",
        client=None,
        parser=None,
        decoder=FakeDecoder(),
    )
    day = date(2026, 5, 1)
    rollup = Rollup(feeds=[feed], landing_dir=landing_dir, curated_dir=curated_dir)
    for path in rollup._expected_outputs(feed, day).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    metadata_calls = []
    data_calls = []
    monkeypatch.setattr(
        rollup, "_rollup_metadata", lambda *a, **kw: metadata_calls.append(1)
    )
    monkeypatch.setattr(rollup, "_rollup_data", lambda *a, **kw: data_calls.append(1))

    rollup.rollup_one("fake-feed", day)

    assert metadata_calls == []
    assert data_calls == []


def test_skip_does_not_happen_when_outputs_missing(tmp_path, monkeypatch):
    landing_dir = tmp_path / "landing"
    curated_dir = tmp_path / "curated"
    feed = Feed(
        name="fake-feed",
        path="/whatever",
        client=None,
        parser=None,
        decoder=FakeDecoder(),
    )
    day = date(2026, 5, 1)
    rollup = Rollup(feeds=[feed], landing_dir=landing_dir, curated_dir=curated_dir)
    metadata_calls = []
    data_calls = []
    monkeypatch.setattr(
        rollup, "_rollup_metadata", lambda *a, **kw: metadata_calls.append(1)
    )
    monkeypatch.setattr(rollup, "_rollup_data", lambda *a, **kw: data_calls.append(1))

    rollup.rollup_one(feed_name="fake-feed", day=day)

    assert metadata_calls == [1]
    assert data_calls == [1]


def test_if_force_true_bypasses_skip(tmp_path, monkeypatch):
    landing_dir = tmp_path / "landing"
    curated_dir = tmp_path / "curated"
    feed = Feed(
        name="fake-feed",
        path="/whatever",
        client=None,
        parser=None,
        decoder=FakeDecoder(),
    )
    day = date(2026, 5, 1)
    rollup = Rollup(feeds=[feed], landing_dir=landing_dir, curated_dir=curated_dir)
    for path in rollup._expected_outputs(feed, day).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    metadata_calls = []
    data_calls = []
    monkeypatch.setattr(
        rollup, "_rollup_metadata", lambda *a, **kw: metadata_calls.append(1)
    )
    monkeypatch.setattr(rollup, "_rollup_data", lambda *a, **kw: data_calls.append(1))

    rollup.rollup_one(feed_name="fake-feed", day=day, force=True)

    assert metadata_calls == [1]
    assert data_calls == [1]


def test_second_run_does_not_redo_work(tmp_path):
    landing_dir = tmp_path / "landing"
    curated_dir = tmp_path / "curated"
    metadata_path = (
        landing_dir
        / "fake-feed"
        / "metadata"
        / "year=2026"
        / "month=5"
        / "day=1"
        / "data.jsonl"
    )
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with open(metadata_path, "a") as f:
        f.write('{"status_code": 200, "fetched_at": 1234567890}\n')
    raw_path = (
        landing_dir
        / "fake-feed"
        / "raw"
        / "year=2026"
        / "month=5"
        / "day=1"
        / "1234567890.bin"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with open(raw_path, "wb") as f:
        f.write(b"\x00\x01\x02\x03")
    feed = Feed(
        name="fake-feed",
        path="/whatever",
        client=None,
        parser=FakeParser(),
        decoder=FakeDecoder(),
    )
    day = date(2026, 5, 1)
    rollup = Rollup(feeds=[feed], landing_dir=landing_dir, curated_dir=curated_dir)

    rollup.run(feed="fake-feed", day=day)  # first run — produces parquet

    # Snapshot every output file's bytes
    snapshot = {p: p.read_bytes() for p in (tmp_path / "curated").rglob("*.parquet")}
    assert snapshot, "first run produced no outputs"  # sanity check

    rollup.run(feed="fake-feed", day=day)  # second run — should skip

    # Bytes should be unchanged
    for path, original_bytes in snapshot.items():
        assert path.read_bytes() == original_bytes, f"{path} was rewritten"
