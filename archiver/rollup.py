from contextlib import ExitStack, contextmanager
import dataclasses
import json
import typing
from dataclasses import asdict
from pathlib import Path
from typing import Iterator
import pyarrow as pa
import pyarrow.json as paj
import pyarrow.parquet as pq
from datetime import date, datetime, timezone
from archiver.decoder import DecodeFailure, TableSpec
from archiver.feed import Feed
from archiver.parser import ParseFailure
from archiver.logger import logger
from archiver.telemetry import Telemetry, NoOpTelemetry
from archiver.writer import FrameError, FrameReader

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
                schema = _schema_for_spec(row_class, spec)
                writers[row_class] = stack.enter_context(
                    self._streaming_writer(path, schema, column_names=spec.column_names)
                )
            if not writers:
                return

            bin_files = (self.landing_dir / feed_name / "raw").glob(
                f"year={day.year}/month={day.month}/day={day.day}/*.bin"
            )
            digest_ts = self._digest_timestamps(
                feed_name, day
            )  # once, before the file loop
            count = 0
            for bin_file in bin_files:
                count += 1
                try:
                    for payload, fetched_at in self._iter_payloads(bin_file, digest_ts):
                        parsed = feed.parser.parse(payload)
                        rows = feed.decoder.decode(parsed, fetched_at=fetched_at)
                        for row in rows:
                            if type(row) not in feed.decoder.produces:
                                logger.warning(
                                    "unexpected row type: %s", type(row).__name__
                                )
                                continue
                            append = writers.get(type(row))
                            if append is None:
                                continue  # known type, but its output already exists
                            append(asdict(row))

                except (ParseFailure, DecodeFailure):
                    logger.warning("skipping malformed .bin: %s", bin_file)
                    continue

            if count == 0:
                logger.warning("no .bin files for %s/%s", feed_name, day)

    def _digest_timestamps(self, feed_name: str, day: date) -> dict[str, int]:
        """Map each stored payload's content digest -> its earliest poll timestamp.

        Built from the day's metadata jsonl (the index): every poll writes a row with
        `timestamp` and `digest`, *including* dedup'd / 304 polls that stored no frame.
        A framed window object only holds DISTINCT payloads, so each frame's digest
        joins here to recover the true per-poll `fetched_at` that the `window=<unix>`
        filename can't carry. If a digest appears on several rows (rare intra-window
        content flap A->B->A collapses to one frame but leaves two rows), keep the
        EARLIEST timestamp.

        Returns {} if the day's metadata file is absent (e.g. a raw-only partition).
        """
        metadata_path = (
            self.landing_dir
            / feed_name
            / "metadata"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
            / "data.jsonl"
        )

        if not metadata_path.exists():
            return {}

        digest_timestamps: dict[str, int] = {}

        with metadata_path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue

                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Skipping malformed metadata row %s:%d: %s",
                        metadata_path,
                        lineno,
                        exc,
                    )
                    continue

                digest = row.get("digest")
                raw_ts = row.get("timestamp")

                # A digest-less row is expected, not malformed: transport-error and
                # other non-payload rows carry no digest (nothing was stored), so they
                # are simply not join candidates. Skip silently — warning here turned a
                # routine feed outage (e.g. a DNS blip) into WARNING-level log spam.
                if digest is None or raw_ts is None:
                    continue

                # Match legacy `int(float(stem))` coercion used when timestamps
                # were embedded in window filenames, so Phase C golden parquet parity holds.
                ts = int(float(raw_ts))

                if digest not in digest_timestamps or ts < digest_timestamps[digest]:
                    digest_timestamps[digest] = ts

        return digest_timestamps

    def _iter_payloads(
        self, bin_file: Path, digest_ts: dict[str, int]
    ) -> Iterator[tuple[bytes, int]]:
        """Yield (payload_bytes, fetched_at) for one raw .bin file, format-agnostic.

        Two on-disk shapes coexist (the cutover day has both):
          * legacy LocalWriter  -> filename stem IS the wall-clock ts; the whole file
            is ONE payload.
          * BatchingWriter      -> filename `window=<unix>`; the file is `\\x89GRT` +
            N framed payloads. `FrameReader` yields (payload, raw-digest-bytes); the
            metadata digest is a hex string, so `.hex()` the frame digest to join into
            `digest_ts`. Fallback when a digest is missing: the window-start unix in
            the stem (coarse, but never crashes).

        Keeping this the single source of "how to get payloads out of a file" lets the
        parse -> decode -> append loop in _rollup_data stay format-agnostic.
        """
        stem = bin_file.stem

        if stem.startswith("window="):
            # --- BatchingWriter framed file ---
            # Stem is "window=<unix>"; parse the fallback timestamp from it.
            try:
                window_start = int(stem.split("=", 1)[1])
            except (IndexError, ValueError) as exc:
                raise ValueError(
                    f"Cannot parse window timestamp from filename {bin_file.name!r}"
                ) from exc

            try:
                fh = bin_file.open("rb")
            except OSError as exc:
                raise OSError(f"Failed to open framed bin file {bin_file}") from exc

            with fh:
                try:
                    reader = FrameReader(fh)
                    for payload, raw_digest in reader:
                        fetched_at = digest_ts.get(raw_digest.hex(), window_start)
                        yield payload, fetched_at
                except (FrameError, EOFError) as exc:
                    logger.warning(
                        "Truncated or corrupt frame in %s (window_start=%d); skipping remainder: %s",
                        bin_file,
                        window_start,
                        exc,
                    )
                    # generator just stops yielding for this file

        else:
            # --- Legacy LocalWriter file ---
            # The entire file is one payload; the stem IS the wall-clock timestamp.
            try:
                fetched_at = int(float(stem))
            except ValueError as exc:
                raise ValueError(
                    f"Cannot parse legacy timestamp from filename {bin_file.name!r}"
                ) from exc

            yield bin_file.read_bytes(), fetched_at

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
    def _streaming_writer(
        path: Path,
        schema: pa.Schema,
        column_names: dict[str, str] | None = None,
        batch_size: int = 5_000,
    ):
        tmp = path.with_suffix(".parquet.tmp")
        writer: pq.ParquetWriter | None = None
        buffer: list[dict] = []
        rename = column_names or {}

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
            if rename:
                row = {rename.get(k, k): v for k, v in row.items()}
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
