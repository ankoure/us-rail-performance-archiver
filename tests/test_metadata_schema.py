import hashlib
import json
import logging
import math
from datetime import date
from types import SimpleNamespace

import pyarrow as pa
import pytest
from google.transit.gtfs_realtime_pb2 import FeedMessage

from analysis.alert_text import _read_polls
from archiver import response
from archiver.metadata_schema import (
    MAX_SKIPPED_LINES,
    METADATA_SCHEMA,
    METADATA_SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SKIPPED_LINES_KEY,
    metadata_table,
    parse_metadata_lines,
    to_us,
)
from archiver.response import (
    ArchivableEvent,
    DecodeFailureResponse,
    DuplicateResponse,
    ErrorResponse,
    FeedResponse,
    JsonResponse,
    NotModifiedResponse,
    ProtobufResponse,
    TransportErrorResponse,
    UnknownResponse,
)
from archiver.source import _poll_key


# --------------------------------------------------------------------------- #
# The producer: every ArchivableEvent subclass vs the schema
# --------------------------------------------------------------------------- #
class FakeHttp:
    """Just enough of an httpx.Response for the response classes."""

    def __init__(self, status_code=200, content=b"payload", content_type="x"):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.content = content
        self.text = content.decode("utf-8", "replace")


# One way to build each response class. A new subclass with no entry here
# fails test_every_response_subclass_has_a_factory, which is the point: it
# forces whoever adds it to check its row against the schema.
FACTORIES = {
    FeedResponse: lambda: FeedResponse(FakeHttp()),
    TransportErrorResponse: lambda: TransportErrorResponse("ConnectTimeout", "boom"),
    ProtobufResponse: lambda: ProtobufResponse(FakeHttp(), FeedMessage()),
    JsonResponse: lambda: JsonResponse(FakeHttp(content=b"{}"), {}),
    ErrorResponse: lambda: ErrorResponse(FakeHttp(status_code=503, content=b"down")),
    DuplicateResponse: lambda: DuplicateResponse(FakeHttp()),
    NotModifiedResponse: lambda: NotModifiedResponse(
        FakeHttp(status_code=304, content=b""), "ab" * 32
    ),
    UnknownResponse: lambda: UnknownResponse(FakeHttp(content_type="text/html")),
    DecodeFailureResponse: lambda: DecodeFailureResponse(
        FakeHttp(), SimpleNamespace(missing_required={"b", "a"}, extras={"x"})
    ),
}


def _response_subclasses(cls=ArchivableEvent):
    """Every subclass defined in archiver.response, recursively. Restricted to
    that module so a fake subclass in some other test can't make this flaky."""
    for sub in cls.__subclasses__():
        if sub.__module__ == response.__name__:
            yield sub
        yield from _response_subclasses(sub)


def test_every_response_subclass_has_a_factory():
    missing = {c.__name__ for c in _response_subclasses()} - {
        c.__name__ for c in FACTORIES
    }
    assert not missing, f"add a FACTORIES entry for: {sorted(missing)}"


@pytest.mark.parametrize("cls", FACTORIES, ids=lambda c: c.__name__)
def test_response_row_keys_are_schema_columns(cls):
    row = FACTORIES[cls]().to_metadata_row()
    assert set(row) <= set(METADATA_SCHEMA.names)


@pytest.mark.parametrize("cls", FACTORIES, ids=lambda c: c.__name__)
def test_response_row_conforms_to_the_schema(cls):
    table = metadata_table([FACTORIES[cls]().to_metadata_row()])
    assert table.num_rows == 1
    assert table.schema.equals(METADATA_SCHEMA)  # ignores key-value metadata


# --------------------------------------------------------------------------- #
# The schema itself
# --------------------------------------------------------------------------- #
def test_timestamp_is_utc_microseconds_and_required():
    field = METADATA_SCHEMA.field("timestamp")
    assert field.type == pa.timestamp("us", tz="UTC")
    assert not field.nullable


def test_every_other_column_is_nullable():
    required = [f.name for f in METADATA_SCHEMA if not f.nullable]
    assert required == ["timestamp"]


def test_schema_keeps_the_historic_count_columns():
    # Written until 2026-08-21; the rewrite would delete them from the
    # June–August partitions if the schema dropped them.
    for name in ("vehicle_count", "trip_update_count", "alert_count"):
        assert name in METADATA_SCHEMA.names


def test_drift_columns_are_string_lists():
    for name in ("drift_missing_required", "drift_extras"):
        assert METADATA_SCHEMA.field(name).type == pa.list_(pa.string())


# Fingerprint of METADATA_SCHEMA per version. Changing the schema without
# bumping METADATA_SCHEMA_VERSION (or bumping without a schema change) fails
# here. On a deliberate change: bump the version, then add its fingerprint from
# the failure message. Never edit an existing entry.
SCHEMA_FINGERPRINTS = {
    1: "9924294537a1f588cd6d70a46b8bfe9f54e910464d96b53a817f45b7f286a8b4",
}


def test_schema_version_is_pinned_to_the_schema():
    got = hashlib.sha256(METADATA_SCHEMA.to_string().encode()).hexdigest()
    assert SCHEMA_FINGERPRINTS.get(METADATA_SCHEMA_VERSION) == got, (
        f"METADATA_SCHEMA_VERSION={METADATA_SCHEMA_VERSION}, fingerprint {got}"
    )
    assert len(set(SCHEMA_FINGERPRINTS.values())) == len(SCHEMA_FINGERPRINTS)


# --------------------------------------------------------------------------- #
# to_us — one conversion everywhere
# --------------------------------------------------------------------------- #
def test_to_us_keeps_microseconds():
    assert to_us(1788739215.744962) == 1788739215744962


def test_to_us_rounds_sub_microsecond_digits():
    assert to_us(1788739215.7449622) == 1788739215744962


def test_to_us_accepts_ints():
    assert to_us(1000) == 1_000_000_000


@pytest.mark.parametrize(
    "ts", [0.0, 1000, 1788739215.744962, 1788739215.7449622, 1788739215.9999996]
)
def test_to_us_agrees_with_the_dedupe_key_and_alert_text(ts):
    line = json.dumps({"timestamp": ts, "digest": "a"}).encode()
    one_day = SimpleNamespace(read_metadata=lambda feed, day: line + b"\n")
    [(alert_text_us, _)] = _read_polls(one_day, "f", date(2026, 1, 1))
    assert to_us(ts) == _poll_key(line)[0] == alert_text_us


@pytest.mark.parametrize(
    "ts, exc",
    [(math.nan, ValueError), (math.inf, OverflowError), (None, TypeError)],
)
def test_to_us_raises_what_poll_key_catches(ts, exc):
    # _poll_key turns these into "no key" — to_us must keep raising them.
    with pytest.raises(exc):
        to_us(ts)


# --------------------------------------------------------------------------- #
# parse_metadata_lines — unusable lines are skipped and counted
# --------------------------------------------------------------------------- #
GOOD = b'{"timestamp": 100.0, "status_code": 200, "digest": "a"}\n'
TORN = b'{"timestamp": 160.0, "status_co'


def test_parse_returns_rows_and_no_skips():
    rows, skipped = parse_metadata_lines(GOOD + GOOD.replace(b"100.0", b"160.0"))
    assert [r["timestamp"] for r in rows] == [100.0, 160.0]
    assert skipped == 0


def test_parse_ignores_blank_lines_without_counting_them():
    rows, skipped = parse_metadata_lines(b"\n" + GOOD + b"   \n\n")
    assert len(rows) == 1
    assert skipped == 0


def test_parse_skips_a_torn_final_line(caplog):
    # A crash mid-append leaves one torn line; that alone must not fail the day.
    assert MAX_SKIPPED_LINES >= 1
    with caplog.at_level(logging.WARNING):
        rows, skipped = parse_metadata_lines(GOOD + TORN)
    assert len(rows) == 1
    assert skipped == 1
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b'{"status_code": 200, "digest": "a"}', id="no-timestamp"),
        pytest.param(b'{"timestamp": null}', id="null-timestamp"),
        pytest.param(b'{"timestamp": "soon"}', id="string-timestamp"),
        pytest.param(b'{"timestamp": NaN}', id="nan-timestamp"),
        pytest.param(b"[1, 2]", id="not-an-object"),
        pytest.param(b"not json", id="garbage"),
    ],
)
def test_parse_skips_lines_with_no_usable_timestamp(line):
    assert MAX_SKIPPED_LINES >= 1
    rows, skipped = parse_metadata_lines(GOOD + line + b"\n")
    assert len(rows) == 1
    assert skipped == 1


def test_parse_tolerates_exactly_max_skipped_lines():
    rows, skipped = parse_metadata_lines(GOOD + b"bad\n" * MAX_SKIPPED_LINES)
    assert skipped == MAX_SKIPPED_LINES


def test_parse_fails_past_max_skipped_lines():
    # Many bad lines is a format problem, not a torn append.
    with pytest.raises(ValueError):
        parse_metadata_lines(GOOD + b"bad\n" * (MAX_SKIPPED_LINES + 1))


def test_parse_keeps_unknown_keys_for_metadata_table_to_reject():
    # Skipping is only for lines that aren't rows. A valid row with an unknown
    # key is a code problem, and it's metadata_table's job to fail on it.
    rows, skipped = parse_metadata_lines(b'{"timestamp": 1.0, "surprise": 1}\n')
    assert rows == [{"timestamp": 1.0, "surprise": 1}]
    assert skipped == 0


# --------------------------------------------------------------------------- #
# metadata_table — conform rows to the schema
# --------------------------------------------------------------------------- #
OK_ROW = {
    "timestamp": 100.0,
    "content_type": "application/x-protobuf",
    "status_code": 200,
    "response_type": "ProtobufResponse",
    "digest": "a",
}
ERROR_ROW = {
    "timestamp": 160.0,
    "error_type": "ConnectTimeout",
    "error_message": "boom",
}


def test_error_free_and_error_days_get_the_same_schema():
    # The drift this fixes: error columns used to exist only on error days.
    assert metadata_table([OK_ROW]).schema.equals(
        metadata_table([OK_ROW, ERROR_ROW]).schema
    )


def test_column_order_is_fixed_whatever_the_key_order():
    # window= slices are written with sort_keys; hour= and data.jsonl aren't.
    sorted_row = dict(sorted(OK_ROW.items()))
    assert metadata_table([sorted_row]).column_names == METADATA_SCHEMA.names


def test_missing_columns_are_typed_nulls():
    table = metadata_table([OK_ROW])
    assert table.column("error_type").to_pylist() == [None]
    assert table.schema.field("error_type").type == pa.string()


def test_timestamp_column_is_built_from_to_us():
    ts = 1788739215.7449622
    table = metadata_table([{**OK_ROW, "timestamp": ts}])
    assert table.column("timestamp").cast(pa.int64()).to_pylist() == [to_us(ts)]


def test_historic_counts_and_drift_lists_keep_their_values():
    june = {**OK_ROW, "vehicle_count": 3, "trip_update_count": 0, "alert_count": 1}
    drift = {**OK_ROW, "drift_missing_required": ["a", "b"], "drift_extras": []}
    table = metadata_table([june, drift])
    assert table.column("vehicle_count").to_pylist() == [3, None]
    assert table.column("drift_missing_required").to_pylist() == [None, ["a", "b"]]


def test_unknown_key_fails_and_names_the_key():
    with pytest.raises(ValueError, match="surprise"):
        metadata_table([{**OK_ROW, "surprise": 1}])


def test_wrong_typed_value_for_a_known_key_fails():
    with pytest.raises((ValueError, TypeError)):
        metadata_table([{**OK_ROW, "status_code": "two hundred"}])


def test_key_value_metadata_carries_version_and_skips():
    meta = metadata_table([OK_ROW], skipped=3).schema.metadata
    assert meta[SCHEMA_VERSION_KEY] == str(METADATA_SCHEMA_VERSION).encode()
    assert meta[SKIPPED_LINES_KEY] == b"3"


def test_skipped_defaults_to_zero():
    assert metadata_table([OK_ROW]).schema.metadata[SKIPPED_LINES_KEY] == b"0"


def test_no_rows_is_an_empty_table_with_the_schema():
    table = metadata_table([])
    assert table.num_rows == 0
    assert table.schema.equals(METADATA_SCHEMA)


def test_rows_may_be_a_one_shot_generator():
    assert metadata_table(r for r in [OK_ROW, ERROR_ROW]).num_rows == 2
