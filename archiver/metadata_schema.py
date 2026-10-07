"""Declared schema for the curated `metadata` table: one row per poll attempt.

The schema describes what `ArchivableEvent.to_metadata_row()` emits across the
whole response hierarchy (archiver/response.py), plus the entity-count columns
the poller wrote until 2026-08-21, so rewritten June-August partitions keep
them. tests/test_metadata_schema.py walks every ArchivableEvent subclass
against it, so a subclass that adds a field without a column fails CI.

Before this, `_rollup_metadata` inferred a schema per feed-day with
`paj.read_json`: columns came and went with the day's rows, `timestamp` was a
float, and column order flipped to alphabetical once window slices (written
with sort_keys) took over. A partition with no `schema_version` in its
key-value metadata is from that inferred era.

Two kinds of bad input, handled differently:
  * a line that isn't a usable row (won't parse, no usable timestamp) is
    isolated damage, most likely a torn final append. It is skipped, counted,
    and the count is stored with the table, up to MAX_SKIPPED_LINES.
  * a valid row the schema doesn't describe (unknown key, wrong-typed value)
    is a code problem. It raises, the feed-day's rollup fails and writes no
    marker, and landing is kept until a fixed rollup retries it.
"""

from __future__ import annotations

import json
from typing import Iterable

import pyarrow as pa

from archiver.logger import logger

# Bump on ANY change to METADATA_SCHEMA, including adding a nullable column,
# and on any change to how a column's value is derived. The rewrite selects
# stale partitions by schema_version < METADATA_SCHEMA_VERSION.
METADATA_SCHEMA_VERSION = 1

METADATA_SCHEMA = pa.schema(
    [
        # The only required column: a line without a usable timestamp is not
        # a row (see parse_metadata_lines).
        pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("content_type", pa.string()),
        pa.field("status_code", pa.int64()),
        pa.field("response_type", pa.string()),
        pa.field("digest", pa.string()),
        # Historic: written by the poller until 2026-08-21, null after.
        pa.field("vehicle_count", pa.int64()),
        pa.field("trip_update_count", pa.int64()),
        pa.field("alert_count", pa.int64()),
        pa.field("error_type", pa.string()),
        pa.field("error_message", pa.string()),
        pa.field("error_body", pa.string()),
        pa.field("drift_missing_required", pa.list_(pa.string())),
        pa.field("drift_extras", pa.list_(pa.string())),
    ]
)

# Key-value metadata keys stored on every written table.
SCHEMA_VERSION_KEY = b"schema_version"
SKIPPED_LINES_KEY = b"skipped_lines"

# More unusable lines than this in one feed-day means a format problem, not a
# torn append: fail the day. Only data.jsonl appends can tear (one line per
# poller crash); window= slices and hour= objects are written whole. Three
# leaves room for a few crashes in a day without hiding a broken writer.
MAX_SKIPPED_LINES = 3

# How much of an unusable line goes into the warning.
_LOGGED_LINE_BYTES = 200


def to_us(ts: float) -> int:
    """Epoch seconds -> integer microseconds, rounded half-to-even.

    The one conversion behind the metadata dedupe key (archiver/source.py),
    alert_text's poll times, and this table's `timestamp` column, so a poll
    has the same µs value everywhere by construction.
    """
    return round(ts * 1_000_000)


def _usable_row(line: bytes) -> dict | None:
    """The line as a row dict, or None if it isn't one: not JSON, not an
    object, or no numeric, finite `timestamp`."""
    try:
        row = json.loads(line)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both
        return None
    if not isinstance(row, dict):
        return None
    ts = row.get("timestamp")
    # bool is an int subclass; a str would survive `ts * 1_000_000` as
    # repetition. Neither is a timestamp.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        to_us(ts)
    except (ValueError, OverflowError):  # NaN, ±Infinity
        return None
    return row


def parse_metadata_lines(data: bytes) -> tuple[list[dict], int]:
    """Parse a feed-day's metadata jsonl into (rows, skipped_count).

    Blank lines are ignored and not counted. A line that won't parse as a JSON
    object, or has no usable `timestamp`, is skipped with a warning and
    counted. Raises ValueError if the count passes MAX_SKIPPED_LINES.
    """
    rows: list[dict] = []
    skipped = 0
    for lineno, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        row = _usable_row(line)
        if row is None:
            skipped += 1
            logger.warning(
                "Skipping unusable metadata line %d: %r",
                lineno,
                line[:_LOGGED_LINE_BYTES],
            )
            continue
        rows.append(row)
    if skipped > MAX_SKIPPED_LINES:
        raise ValueError(
            f"{skipped} unusable metadata lines (limit {MAX_SKIPPED_LINES}): "
            "more than torn appends can explain"
        )
    return rows, skipped


def metadata_table(rows: Iterable[dict], *, skipped: int = 0) -> pa.Table:
    """Conform poll rows to METADATA_SCHEMA.

    Missing columns become typed nulls; columns come out in schema order
    whatever the rows' key order; `timestamp` is built from to_us(). Raises
    ValueError on a key the schema doesn't have; a wrong-typed value for a
    known key raises from pyarrow. Stores SCHEMA_VERSION_KEY and
    SKIPPED_LINES_KEY in the key-value metadata.

    Shared by _rollup_metadata (new days) and the one-off hot rewrite (old
    partitions), so both produce identical tables by construction.
    """
    names = METADATA_SCHEMA.names
    known = set(names)
    columns: dict[str, list] = {name: [] for name in names}
    for row in rows:
        unknown = row.keys() - known
        if unknown:
            raise ValueError(
                f"metadata row has keys not in METADATA_SCHEMA: {sorted(unknown)}"
            )
        for name in names:
            columns[name].append(row.get(name))

    # Integer µs straight into the timestamp type: no float ever reaches
    # pyarrow, so its rounding rules never get a say.
    columns["timestamp"] = [to_us(ts) for ts in columns["timestamp"]]

    schema = METADATA_SCHEMA.with_metadata(
        {
            SCHEMA_VERSION_KEY: str(METADATA_SCHEMA_VERSION).encode(),
            SKIPPED_LINES_KEY: str(skipped).encode(),
        }
    )
    arrays = [pa.array(columns[field.name], type=field.type) for field in schema]
    return pa.Table.from_arrays(arrays, schema=schema)
