from contextlib import contextmanager
import dataclasses
import typing
from dataclasses import asdict
from functools import reduce
import operator
from pathlib import Path
from typing import Iterator
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.dataset as ds
from datetime import date, datetime, timezone
from archiver.decoder import (
    AlertRow,
    DecodeFailure,
    StandardDecoder,
    StopTimeUpdateRow,
    VehicleRow,
)
from archiver.logger import logger

_PY_TO_ARROW = {
    int: pa.int64(),
    str: pa.string(),
    float: pa.float64(),
    bool: pa.bool_(),
}


def _schema_from_dataclass(cls: type) -> pa.Schema:
    hints = typing.get_type_hints(cls)
    fields = []
    for f in dataclasses.fields(cls):
        py_type = _unwrap_optional(hints[f.name])
        fields.append(pa.field(f.name, _PY_TO_ARROW[py_type], nullable=True))
    return pa.schema(fields)


def _unwrap_optional(annotation):
    """Given int | None or Optional[int], return int."""
    args = typing.get_args(annotation)
    non_none = [a for a in args if a is not type(None)]
    return non_none[0] if non_none else annotation


class Rollup:
    def __init__(
        self,
        base_dir: Path,
        curated_dir: Path,
    ) -> None:
        self.base_dir = base_dir
        self.curated_dir = curated_dir
        self.decoder = StandardDecoder()  # TODO: per-feed dispatch when MTA arrives

    def run(self) -> None:
        for feed_name, day in self._discover():
            self.rollup_one(feed_name, day)

    def rollup_one(self, feed_name: str, day: date) -> None:
        self._rollup_metadata(feed_name, day)
        self._rollup_data(feed_name, day)

    def _discover(self) -> Iterator[tuple[str, date]]:
        """Yield (feed_name, day) for every metadata partition older than today UTC."""
        today = datetime.now(timezone.utc).date()
        for metadata_dir in self.base_dir.glob("*/metadata"):
            feed_name = metadata_dir.parent.name
            for jsonl_path in metadata_dir.rglob("data.jsonl"):
                partitions = {
                    part.split("=")[0]: int(part.split("=")[1])
                    for part in jsonl_path.parts
                    if "=" in part
                }
                day = date(partitions["year"], partitions["month"], partitions["day"])
                if day < today:
                    yield feed_name, day

    def _rollup_metadata(self, feed_name: str, day: date) -> None:
        root = self.base_dir / feed_name / "metadata"
        table = ds.dataset(root, format="json", partitioning="hive").to_table(
            filter=_build_filter(day.year, day.month, day.day)
        )
        table = table.drop_columns(["year", "month", "day"])
        out_path = self._curated_path("metadata", feed_name, day)
        if table.num_rows > 0:
            self._write_parquet(table, out_path)
        else:
            logger.warning("nothing to roll up for %s/%s", feed_name, day)

    def _rollup_data(self, feed_name: str, day: date) -> None:
        paths = {
            "vehicles": self._curated_path("vehicles", feed_name, day),
            "trip_updates": self._curated_path("trip_updates", feed_name, day),
            "alerts": self._curated_path("alerts", feed_name, day),
        }
        schemas = {
            "vehicles": _schema_from_dataclass(VehicleRow),
            "trip_updates": _schema_from_dataclass(StopTimeUpdateRow),
            "alerts": _schema_from_dataclass(AlertRow),
        }

        with (
            self._streaming_writer(paths["vehicles"], schemas["vehicles"]) as write_v,
            self._streaming_writer(
                paths["trip_updates"], schemas["trip_updates"]
            ) as write_t,
            self._streaming_writer(paths["alerts"], schemas["alerts"]) as write_a,
        ):
            for bin_file in _iter_partition_files(
                self.base_dir / feed_name / "raw",
                "*.bin",
                _partition_filters(day.year, day.month, day.day),
            ):
                try:
                    rows = self.decoder.decode(bin_file.read_bytes())
                except DecodeFailure:
                    logger.warning("skipping malformed .bin: %s", bin_file)
                    continue

                for row in rows:
                    if isinstance(row, VehicleRow):
                        write_v(asdict(row))
                    elif isinstance(row, StopTimeUpdateRow):
                        write_t(asdict(row))
                    elif isinstance(row, AlertRow):
                        write_a(asdict(row))
                    else:
                        logger.warning("unexpected row type: %s", type(row).__name__)

    @staticmethod
    def _write_parquet(table: pa.Table, path: Path) -> None:
        tmp = path.with_suffix(".parquet.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, tmp)
        tmp.rename(path)

    def _curated_path(self, kind: str, feed_name: str, day: date) -> Path:
        return (
            self.curated_dir
            / kind
            / f"feed={feed_name}"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
            / "data.parquet"
        )

    @staticmethod
    @contextmanager
    def _streaming_writer(path: Path, schema: pa.Schema, batch_size: int = 10_000):
        tmp = path.with_suffix(".parquet.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)

        writer: pq.ParquetWriter | None = None
        buffer: list[dict] = []

        def flush():
            nonlocal writer
            if not buffer:
                return
            if writer is None:
                writer = pq.ParquetWriter(tmp, schema)
            writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
            buffer.clear()

        def append(row: dict):
            buffer.append(row)
            if len(buffer) >= batch_size:
                flush()

        try:
            yield append
            flush()
        finally:
            if writer is not None:
                writer.close()
                if tmp.exists():
                    tmp.rename(path)
                else:
                    logger.error("writer was active but tmp file missing: %s", tmp)
            elif tmp.exists():
                tmp.unlink()


def _build_filter(year, month, day):
    """Build a PyArrow filter expression from optional partition args."""
    filters = []
    if year is not None:
        filters.append(ds.field("year") == year)
    if month is not None:
        filters.append(ds.field("month") == month)
    if day is not None:
        filters.append(ds.field("day") == day)

    return reduce(operator.and_, filters) if filters else None


def _partition_filters(year, month, day) -> dict:
    """For manual path filtering when PyArrow can't handle the format."""
    return {
        k: v
        for k, v in {"year": year, "month": month, "day": day}.items()
        if v is not None
    }


def _iter_partition_files(root: Path, pattern: str, filters: dict):
    """Walk hive partitions and yield files matching the filter criteria."""
    for f in root.rglob(pattern):
        partitions = {
            part.split("=")[0]: int(part.split("=")[1])
            for part in f.parts
            if "=" in part
        }
        if all(partitions.get(k) == v for k, v in filters.items()):
            yield f
