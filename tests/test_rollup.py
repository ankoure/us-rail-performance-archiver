from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterator
import pyarrow.parquet as pq
import pyarrow as pa
from archiver.decoder import VehicleRow
from archiver.rollup import _schema_from_dataclass, Rollup
import pytest
from archiver.decoder import Row, Decoder, TableSpec


@dataclass
class FakeRow(Row):
    feed_timestamp: int
    destination: str
    direction: str


class FakeDecoder(Decoder):
    produces = {FakeRow: TableSpec("fakes")}

    def decode(self, raw: bytes, *, fetched_at: int | None = None) -> Iterator[Row]:

        for _ in range(5):
            feed_timestamp = datetime.now()
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
