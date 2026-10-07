import io

import pyarrow as pa
import pyarrow.json as paj
import pytest

from archiver.metadata_schema import (
    METADATA_SCHEMA,
    SCHEMA_VERSION_KEY,
    SKIPPED_LINES_KEY,
    metadata_table,
    parse_metadata_lines,
)
from archiver.source import dedupe_metadata_lines, select_metadata_names
from pipeline.rewrite_metadata_schema import (
    DUPLICATES_REMOVED_KEY,
    REWRITTEN_FROM_KEY,
    _day_of,
    _feed_of,
    conform,
    partition_version,
)

# A 9/7-style landing day: the hour= file in json.dumps defaults, the window=
# re-ship of the same polls compact with sort_keys, plus a transport error and
# a 304 that repeats a digest at a later time.
HOUR = (
    b'{"timestamp": 1788739215.744962, "content_type": "application/x-protobuf", '
    b'"status_code": 200, "response_type": "ProtobufResponse", "digest": "aaa"}\n'
    b'{"timestamp": 1788739245.1, "error_type": "ConnectTimeout", "error_message": "x"}\n'
)
WINDOW = (
    b'{"content_type":"application/x-protobuf","digest":"aaa",'
    b'"response_type":"ProtobufResponse","status_code":200,"timestamp":1788739215.744962}\n'
    b'{"error_message":"x","error_type":"ConnectTimeout","timestamp":1788739245.1}\n'
    b'{"content_type":"application/x-protobuf","digest":"aaa",'
    b'"response_type":"NotModifiedResponse","status_code":304,"timestamp":1788739275.25}\n'
)
FILES = {"hour=1788739200.jsonl": HOUR, "window=1788739200.jsonl": WINDOW}


def _old_rollup(files: dict[str, bytes]) -> pa.Table:
    """What the hot partition holds today: S3Source's sorted plain join, then
    _rollup_metadata's inferred paj.read_json."""
    return paj.read_json(io.BytesIO(b"".join(files[k] for k in sorted(files))))


def _new_rollup(files: dict[str, bytes]) -> pa.Table:
    """What the rollup builds now from the same landing day."""
    data = dedupe_metadata_lines(files[k] for k in select_metadata_names(files))
    rows, skipped = parse_metadata_lines(data)
    return metadata_table(rows, skipped=skipped)


def test_rewritten_partition_equals_what_the_rollup_builds_now():
    old = _old_rollup(FILES)
    assert old.num_rows == 5  # the duplicates the rewrite has to remove

    rewritten, duplicates, skipped = conform(old, version=0)

    assert rewritten.equals(_new_rollup(FILES))  # data and schema, not kv metadata
    assert (duplicates, skipped) == (2, 0)


def test_rewrite_records_its_audit_trail_in_key_value_metadata():
    rewritten, _, _ = conform(_old_rollup(FILES), version=0)
    meta = rewritten.schema.metadata
    assert meta[SCHEMA_VERSION_KEY] == b"1"
    assert meta[SKIPPED_LINES_KEY] == b"0"
    assert meta[REWRITTEN_FROM_KEY] == b"0"
    assert meta[DUPLICATES_REMOVED_KEY] == b"2"


def test_rewrite_fixes_alphabetical_column_order_and_missing_columns():
    # 9/20-era partition: sorted columns, no error columns at all.
    old = _old_rollup({"window=1.jsonl": WINDOW.splitlines(keepends=True)[0]})
    assert old.column_names == sorted(old.column_names)
    rewritten, _, _ = conform(old, version=0)
    assert rewritten.schema.equals(METADATA_SCHEMA)


def test_rewrite_keeps_historic_counts():
    june = b'{"timestamp": 100.5, "digest": "a", "vehicle_count": 3, "trip_update_count": 0, "alert_count": 1}\n'
    rewritten, _, _ = conform(_old_rollup({"data.jsonl": june}), version=0)
    assert rewritten.column("vehicle_count").to_pylist() == [3]
    assert rewritten.column("alert_count").to_pylist() == [1]


def test_rewrite_skips_and_counts_rows_without_a_timestamp():
    old = pa.table(
        {"timestamp": [100.0, None, float("nan")], "digest": ["a", "b", "c"]}
    )
    rewritten, duplicates, skipped = conform(old, version=0)
    assert rewritten.num_rows == 1
    assert (duplicates, skipped) == (0, 2)
    assert rewritten.schema.metadata[SKIPPED_LINES_KEY] == b"2"


def test_rewrite_fails_on_a_column_the_schema_does_not_have():
    old = pa.table({"timestamp": [100.0], "surprise": ["x"]})
    with pytest.raises(ValueError, match="surprise"):
        conform(old, version=0)


def test_rewrite_refuses_a_timestamp_that_is_not_epoch_seconds():
    old = pa.table({"timestamp": pa.array([0], pa.timestamp("us", tz="UTC"))})
    with pytest.raises(ValueError, match="epoch seconds"):
        conform(old, version=0)


def test_partition_version_defaults_to_zero_for_the_inferred_era():
    assert partition_version(pa.schema([("timestamp", pa.float64())])) == 0
    assert partition_version(metadata_table([]).schema) == 1


def test_key_helpers():
    key = "metadata/feed=mbta-alerts/year=2026/month=9/day=7/data.parquet"
    assert _feed_of(key) == "mbta-alerts"
    assert _day_of(key) == "2026-09-07"
