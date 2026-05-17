from contextlib import ExitStack, contextmanager
import dataclasses
import typing
from dataclasses import asdict
from pathlib import Path
from typing import Iterator
import pyarrow as pa
import pyarrow.json as paj
import pyarrow.parquet as pq
from datetime import date, datetime, timezone
from archiver.decoder import DecodeFailure
from archiver.feed import Feed
from archiver.parser import ParseFailure
from archiver.logger import logger
from archiver.telemetry import Telemetry, NoOpTelemetry

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
    _METADATA_KIND = "metadata"

    def __init__(
        self,
        feeds: list[Feed],
        landing_dir: Path,
        curated_dir: Path,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.landing_dir = landing_dir
        self.curated_dir = curated_dir
        self.feeds_by_name = {f.name: f for f in feeds}
        self.telemetry = telemetry or NoOpTelemetry()

    def run(
        self, feed: str | None = None, day: date | None = None, *, force: bool = False
    ) -> None:
        with self.telemetry.span("rollup.run"):
            if feed is not None and feed not in self.feeds_by_name:
                raise ValueError(f"unknown feed: {feed}")
            for feed_name, partition_day in self.discover(feed=feed, day=day):
                self.rollup_one(feed_name, partition_day, force=force)

    def rollup_one(self, feed_name: str, day: date, *, force: bool = False) -> None:
        feed = self.feeds_by_name.get(feed_name)
        if feed is None:
            logger.warning("orphaned data for unknown feed: %s", feed_name)
            return
        expected_outputs = self._expected_outputs(feed=feed, day=day)
        missing = {
            kind: path for kind, path in expected_outputs.items() if not path.exists()
        }
        if not force and not missing:
            self.telemetry.incr("rollup.skipped", tags={"feed": feed_name})
            logger.info("skipping %s/%s — all outputs exist", feed_name, day)
            return
        with self.telemetry.span("rollup.day", tags={"feed": feed_name}):
            self._rollup_metadata(feed_name, day, force=force)
            self._rollup_data(feed, day, force=force)

    def discover(
        self, feed: str | None = None, day: date | None = None
    ) -> Iterator[tuple[str, date]]:
        """Yield (feed_name, day) for every metadata partition older than today UTC,
        optionally filtered to a single feed and/or day."""
        today = datetime.now(timezone.utc).date()
        for metadata_dir in self.landing_dir.glob(f"*/{self._METADATA_KIND}"):
            feed_name = metadata_dir.parent.name
            if feed is not None and feed_name != feed:
                continue
            for jsonl_path in metadata_dir.rglob("data.jsonl"):
                partitions = {
                    part.split("=")[0]: int(part.split("=")[1])
                    for part in jsonl_path.parts
                    if "=" in part
                }
                partition_day = date(
                    partitions["year"], partitions["month"], partitions["day"]
                )
                if partition_day >= today:
                    continue
                if day is not None and partition_day != day:
                    continue
                yield feed_name, partition_day

    def _rollup_metadata(
        self, feed_name: str, day: date, *, force: bool = False
    ) -> None:

        root = self.landing_dir / feed_name / self._METADATA_KIND
        metadata_path = (
            root
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
            / "data.jsonl"
        )
        out_path = self._curated_path(self._METADATA_KIND, feed_name, day)
        if not force and out_path.exists():
            return

        table = paj.read_json(input_file=metadata_path)
        if table.num_rows > 0:
            self._write_parquet(table, out_path)
        else:
            logger.warning("nothing to roll up for %s/%s", feed_name, day)

    def _rollup_data(self, feed: Feed, day: date, *, force: bool = False) -> None:
        feed_name = feed.name
        with ExitStack() as stack:
            writers = {}
            for row_class, spec in feed.decoder.produces.items():
                path = self._curated_path(spec.name, feed_name, day)
                if not force and path.exists():
                    continue
                schema = _schema_from_dataclass(row_class)
                writers[row_class] = stack.enter_context(
                    (self._streaming_writer(path, schema))
                )
            if not writers:
                return

            bin_files = (self.landing_dir / feed_name / "raw").glob(
                f"year={day.year}/month={day.month}/day={day.day}/*.bin"
            )

            count = 0
            for bin_file in bin_files:
                count += 1
                fetched_at = int(float(bin_file.stem))
                try:
                    parsed = feed.parser.parse(bin_file.read_bytes())
                    rows = feed.decoder.decode(parsed, fetched_at=fetched_at)
                except (ParseFailure, DecodeFailure):
                    logger.warning("skipping malformed .bin: %s", bin_file)
                    continue

                for row in rows:
                    if type(row) not in feed.decoder.produces:
                        logger.warning("unexpected row type: %s", type(row).__name__)
                        continue
                    append = writers.get(type(row))
                    if append is None:
                        continue  # known type, but its output already exists
                    append(asdict(row))
            if count == 0:
                logger.warning("no .bin files for %s/%s", feed_name, day)

    def _expected_outputs(self, feed: Feed, day: date) -> dict[str, Path]:
        shape = {
            self._METADATA_KIND: self._curated_path(self._METADATA_KIND, feed.name, day)
        }
        feed_name = feed.name
        for _, spec in feed.decoder.produces.items():
            path = self._curated_path(spec.name, feed_name, day)
            shape[spec.name] = path
        return shape

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
        writer: pq.ParquetWriter | None = None
        buffer: list[dict] = []

        def flush():
            nonlocal writer
            if not buffer:
                return
            if writer is None:
                tmp.parent.mkdir(parents=True, exist_ok=True)
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
