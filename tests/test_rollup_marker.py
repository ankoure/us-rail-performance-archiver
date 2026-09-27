from contextlib import ExitStack, contextmanager
import dataclasses
import io
import json
import typing
from dataclasses import asdict
from pathlib import Path
from typing import Iterator
from archiver.source import Source
import pyarrow as pa
import pyarrow.json as paj
import pyarrow.parquet as pq
from datetime import date, datetime, timezone
from archiver.decoder import (
    DecodeFailure,
    TableSpec,
)
from archiver.feed import Feed
from archiver.parser import ParseFailure
from archiver.logger import logger
from archiver.payloads import digest_timestamps, iter_payloads
from archiver.telemetry import Telemetry, NoOpTelemetry

_PY_TO_ARROW = {
    int: pa.int64(),
    str: pa.string(),
    float: pa.float64(),
    bool: pa.bool_(),
}


def _schema_for_spec(cls: type, spec: TableSpec) -> pa.Schema:
    hints = typing.get_type_hints(cls)
    dataclass_fields = {f.name for f in dataclasses.fields(cls)}
    unknown = set(spec.column_names) - dataclass_fields
    if unknown:
        raise ValueError(
            f"{cls.__name__}.TableSpec column_names references unknown fields: "
            f"{sorted(unknown)}"
        )
    fields = []
    for f in dataclasses.fields(cls):
        py_type = _unwrap_optional(hints[f.name])
        parquet_name = spec.column_names.get(f.name, f.name)
        fields.append(pa.field(parquet_name, _PY_TO_ARROW[py_type], nullable=True))
    for extra in spec.extra_columns:
        fields.append(extra.with_nullable(True))
    return pa.schema(fields)


def _unwrap_optional(annotation):
    """Given int | None or Optional[int], return int."""
    args = typing.get_args(annotation)
    non_none = [a for a in args if a is not type(None)]
    return non_none[0] if non_none else annotation


class _ParquetSink:
    """Streams tables into <path>.tmp and counts the rows written.

    Only commit() moves the tmp file into place, so a data.parquet on disk is
    always a complete file. That's what lets a resumed run trust (and count)
    outputs that already exist.
    """

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self.path = path
        self.schema = schema
        self.rows = 0
        self._tmp = path.with_suffix(".parquet.tmp")
        self._writer: pq.ParquetWriter | None = None

    def write_table(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        if self._writer is None:
            self._tmp.parent.mkdir(parents=True, exist_ok=True)
            self._writer = pq.ParquetWriter(self._tmp, self.schema)
        self._writer.write_table(table)
        self.rows += table.num_rows

    def commit(self) -> None:
        if self._writer is None:
            # Nothing written this run. Any file already at `path` is left over
            # from an earlier run (we only get here if we chose to rewrite it),
            # and keeping it would contradict the marker's count of 0.
            # This only fixes the local tree: a copy already shipped to S3 stays
            # there, since _ship_hot uploads what exists and never deletes.
            self.path.unlink(missing_ok=True)
            return
        self._writer.close()
        self._writer = None
        self._tmp.rename(self.path)

    def abort(self) -> None:
        try:
            if self._writer is not None:
                self._writer.close()
        finally:
            self._writer = None
            self._tmp.unlink(missing_ok=True)


class _RowBuffer:
    """Row-at-a-time front end for a _ParquetSink (the Python decode path)."""

    def __init__(
        self, sink: _ParquetSink, column_names: dict[str, str], batch_size: int
    ) -> None:
        self._sink = sink
        self._rename = column_names
        self._batch_size = batch_size
        self._buffer: list[dict] = []

    @property
    def rows(self) -> int:
        # Rows actually written; final once the context manager has flushed.
        return self._sink.rows

    def append(self, row: dict) -> None:
        if self._rename:
            row = {self._rename.get(k, k): v for k, v in row.items()}
        self._buffer.append(row)
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        self._sink.write_table(
            pa.Table.from_pylist(self._buffer, schema=self._sink.schema)
        )
        self._buffer.clear()


class Rollup:
    _METADATA_KIND = "metadata"
    _MARKER_KIND = "_rollup"
    _MARKER_NAME = "_SUCCESS.json"

    def __init__(
        self,
        feeds: list[Feed],
        source: Source,
        curated_dir: Path,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._source = source
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
        marker = self._marker_path(feed_name, day)
        if not force and marker.exists():
            self.telemetry.incr("rollup.skipped", tags={"feed": feed_name})
            logger.info("skipping %s/%s — marker exists", feed_name, day)
            return
        with self.telemetry.span("rollup.day", tags={"feed": feed_name}):
            # Remove the old marker before touching any output. If this run
            # crashes partway, prune must not find a marker sitting next to
            # half-rewritten files.
            marker.unlink(missing_ok=True)
            meta_rows = self._rollup_metadata(feed_name, day, force=force)
            data_rows = self._rollup_data(feed, day, force=force)
            # Reached only if nothing above raised. Parse/decode failures are
            # handled per .bin inside _rollup_data and don't block the marker.
            self._write_marker(
                feed_name, day, {self._METADATA_KIND: meta_rows, **data_rows}
            )

    def discover(
        self, feed: str | None = None, day: date | None = None
    ) -> Iterator[tuple[str, date]]:
        """Yield (feed_name, day) for every metadata partition older than today UTC,
        optionally filtered to a single feed and/or day."""
        today = datetime.now(timezone.utc).date()

        for feed_name, partition_day in self._source.discover(feed, day):
            if partition_day >= today:
                continue
            yield feed_name, partition_day

    def _rollup_metadata(
        self, feed_name: str, day: date, *, force: bool = False
    ) -> int:
        """Roll up the day's metadata; return the row count now on disk."""
        out_path = self._curated_path(self._METADATA_KIND, feed_name, day)
        if not force and out_path.exists():
            return pq.read_metadata(out_path).num_rows
        data = self._source.read_metadata(feed_name, day)
        table = paj.read_json(io.BytesIO(data)) if data else None
        if table is None or table.num_rows == 0:
            logger.warning("nothing to roll up for %s/%s", feed_name, day)
            # Under force, an older file here would contradict the count of 0.
            out_path.unlink(missing_ok=True)
            return 0
        self._write_parquet(table, out_path)
        return table.num_rows

    def _rollup_data(
        self, feed: Feed, day: date, *, force: bool = False
    ) -> dict[str, int]:
        """Roll up every kind in feed.decoder.produces.

        Returns {kind: rows on disk} for every kind, with 0 for kinds that had
        no rows. Kinds skipped because their file already exists are counted
        from the parquet footer.
        """
        feed_name = feed.name
        rows: dict[str, int] = {}
        rust_decode = None
        if feed.decoder.rust_decode:
            import rail_decoder  # Lazy — only feeds with a Rust path need the extension

            rust_decode = getattr(rail_decoder, feed.decoder.rust_decode)

        sinks: dict[str, _ParquetSink | _RowBuffer] = {}  # kind -> row counter
        with ExitStack() as stack:
            writers: dict[type, _RowBuffer] = {}
            batch_writers: dict[str, tuple[_ParquetSink, dict[str, str]]] = {}
            for row_class, spec in feed.decoder.produces.items():
                path = self._curated_path(spec.name, feed_name, day)
                if not force and path.exists():
                    rows[spec.name] = pq.read_metadata(path).num_rows
                    continue
                schema = _schema_for_spec(row_class, spec)
                if rust_decode:
                    sink = stack.enter_context(
                        self._batch_streaming_writer(path, schema)
                    )
                    batch_writers[spec.name] = (sink, spec.column_names)
                    sinks[spec.name] = sink
                else:
                    buf = stack.enter_context(
                        self._streaming_writer(
                            path, schema, column_names=spec.column_names
                        )
                    )
                    writers[row_class] = buf
                    sinks[spec.name] = buf
            if not sinks:
                return rows

            digest_ts = digest_timestamps(
                self._source, feed_name, day
            )  # once, before the file loop
            count = 0
            for name, blob in self._source.iter_bins(feed_name, day):
                count += 1
                # Only reading and decoding the .bin is inside the try. Writes
                # happen outside it, so an Arrow error on the output side (e.g.
                # pa.ArrowInvalid, a ValueError subclass) fails the day and
                # blocks the marker, instead of being logged as a bad input file.
                decoded_payloads = _decode_bin(feed, rust_decode, name, blob, digest_ts)
                while True:
                    try:
                        decoded = next(decoded_payloads)
                    except StopIteration:
                        break
                    except (ParseFailure, DecodeFailure, ValueError):
                        # Rows from this .bin's earlier payloads are already
                        # written, same as before this change.
                        logger.warning("skipping malformed .bin: %s", name)
                        break
                    if rust_decode:
                        for table_name, rust_batch in decoded.items():
                            entry = batch_writers.get(table_name)
                            if entry is None or rust_batch.num_rows == 0:
                                continue
                            sink, column_names = entry
                            sink.write_table(
                                _batch_to_parquet_table(
                                    rust_batch, sink.schema, column_names
                                )
                            )
                    else:
                        for row in decoded:
                            if type(row) not in feed.decoder.produces:
                                logger.warning(
                                    "unexpected row type: %s", type(row).__name__
                                )
                                continue
                            buf = writers.get(type(row))
                            if buf is None:
                                continue
                            buf.append(asdict(row))

            if count == 0:
                logger.warning("no .bin files for %s/%s", feed_name, day)

        # The ExitStack has flushed and committed every writer, so the counts are final.
        rows.update({kind: s.rows for kind, s in sinks.items()})
        return rows

    def _write_marker(self, feed_name: str, day: date, rows: dict[str, int]) -> None:
        """Write _SUCCESS.json: this (feed, day) rolled up completely.

        rows[kind] is the number of rows rollup wrote for that kind (0 means no
        file). It's a rollup-time count, not a description of what's in S3
        later: compact_trip_updates rewrites trip_updates in place between
        rollup and hot-ship, often to far fewer rows, and the marker isn't
        updated. Don't reconcile these counts against shipped parquet.
        """
        path = self._marker_path(feed_name, day)
        tmp = path.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "feed": feed_name,
            "day": day.isoformat(),
            "rows": rows,
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            tmp.write_text(json.dumps(body) + "\n")
            tmp.rename(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_parquet(table: pa.Table, path: Path) -> None:
        tmp = path.with_suffix(".parquet.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            pq.write_table(table, tmp)
            tmp.rename(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _partition_dir(self, kind: str, feed_name: str, day: date) -> Path:
        return (
            self.curated_dir
            / kind
            / f"feed={feed_name}"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
        )

    def _curated_path(self, kind: str, feed_name: str, day: date) -> Path:
        return self._partition_dir(kind, feed_name, day) / "data.parquet"

    def _marker_path(self, feed_name: str, day: date) -> Path:
        return (
            self._partition_dir(self._MARKER_KIND, feed_name, day) / self._MARKER_NAME
        )

    @staticmethod
    @contextmanager
    def _streaming_writer(
        path: Path,
        schema: pa.Schema,
        column_names: dict[str, str] | None = None,
        batch_size: int = 5_000,
    ) -> Iterator[_RowBuffer]:
        sink = _ParquetSink(path, schema)
        buf = _RowBuffer(sink, column_names or {}, batch_size)
        try:
            yield buf
            buf.flush()
        except BaseException:
            sink.abort()
            raise
        sink.commit()

    @staticmethod
    @contextmanager
    def _batch_streaming_writer(
        path: Path, schema: pa.Schema
    ) -> Iterator[_ParquetSink]:
        sink = _ParquetSink(path, schema)
        try:
            yield sink
        except BaseException:
            sink.abort()
            raise
        sink.commit()


def _decode_bin(feed: Feed, rust_decode, name: str, blob: bytes, digest_ts):
    """Yield one decoded result per payload in a .bin: a {table: RecordBatch}
    dict on the Rust path, a list of row dataclasses on the Python path.

    Everything that can fail because the input is bad happens in here, so the
    caller can catch input errors around next() without also catching errors
    from its own writes.
    """
    for payload, fetched_at in iter_payloads(name, blob, digest_ts):
        if rust_decode:
            yield rust_decode(payload)
        else:
            parsed = feed.parser.parse(payload)
            # list(): decode may be lazy, and its errors must surface here.
            yield list(feed.decoder.decode(parsed, fetched_at=fetched_at))


def _batch_to_parquet_table(
    batch: pa.RecordBatch, schema: pa.Schema, column_names: dict[str, str]
) -> pa.Table:
    """Rename batch columns per column_names, and add any schema columns
    (e.g. TableSpec.extra_columns) missing from the batch as all-null."""
    rename = {
        v: k for k, v in column_names.items()
    }  # parquet name -> rust/python field name
    arrays = []
    for field in schema:
        source_name = rename.get(field.name, field.name)
        if source_name in batch.schema.names:
            arrays.append(batch.column(source_name))
        else:
            arrays.append(pa.nulls(batch.num_rows, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)
