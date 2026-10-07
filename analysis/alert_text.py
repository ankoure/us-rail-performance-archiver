"""Versioned alert text: every run of every translation, for lost-in-transit.

Three day-partitioned tables, built from a (feed, day)'s raw landing polls
(see docs/design/alert-translations.md for the full design):

  alert_text      one row per *run* of one exact text for one
                  (alert, TranslatedString field, language)
  alert_versions  one row per run of one set of non-text metadata per alert
                  (cause/effect/severity, active_period, informed_entity, ...)
  alert_payloads  one row per distinct payload digest the day's polls
                  referenced: header timestamp, entity counts, parse outcome

A run is a maximal stretch of consecutive *successfully parsed* polls in which
the key had one value. A parsed poll that shows a different value, or doesn't
observe the key at all, ends it. So A->B->A is three runs, and an alert that
disappears and comes back starts a new one. Polls that fail (transport/HTTP
errors, undecodable payloads, digests we can't resolve) are not evidence
either way, so they don't end a run -- they surface through `spans_gap` and the
poll log (`metadata` joined to `alert_payloads` on digest) instead.

Why the fold walks the metadata poll log rather than the payloads: landing is
content-addressed. A day's raw bins hold each DISTINCT payload once, and
archiver.payloads.digest_timestamps keeps only a digest's EARLIEST fetched_at.
Iterating payloads (which alert_snapshot does) therefore can't see A->B->A --
the second A is the same stored bin. The metadata jsonl is the only per-poll
record: one row per poll, including 304s and duplicates, each carrying the
digest of the content it (re)served. So pass 1 decodes each distinct payload
once into compact interned state keyed by sha256(payload) -- the same digest
the poller writes (archiver/response.py) -- and pass 2 folds runs over the
metadata rows in time order.

Day boundaries: poll state (ETag, last digest) survives midnight in the poller,
so a day's first duplicate/304 polls reference a payload that was stored on a
PREVIOUS day and isn't in this day's bins. The builder resolves that digest
from the previous day's own output: the runs still open at its end are exactly
the content of its last parsed poll. The same mechanism carries `run_id`s
across midnight, which is why days must be built in ascending order per feed.
A day with no previous-day output (downtime, or the first day built) starts
fresh.

Memory: each payload is reduced to its alert entities immediately, and both
alert bodies and whole-payload alert content are interned by digest, so state
is O(distinct alert bodies + distinct content states), not O(polls x alerts).
Same rule as analysis.alert_snapshot: never hold a day of raw bytes or decoded
FeedMessages (combined feeds carry far more trip/vehicle entities than alerts).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from google.protobuf import unknown_fields
from google.protobuf.empty_pb2 import Empty
from google.transit.gtfs_realtime_pb2 import (
    Alert,
    FeedHeader,
    TranslatedString,
    TripDescriptor,
)
from pyarrow.fs import FileType

from analysis.curated_fs import curated_fs
from archiver.feed import Feed
from archiver.logger import logger
from archiver.metadata_schema import parse_metadata_lines, to_us
from archiver.parser import ParseFailure
from archiver.payloads import digest_timestamps, iter_payloads
from archiver.source import Source

SCHEMA_VERSION = 1
TABLES = ("alert_text", "alert_versions", "alert_payloads")

# Used for gap detection when a feed has no poll_interval_seconds of its own.
DEFAULT_POLL_INTERVAL_SECONDS = 30
# Consecutive poll-log rows further apart than this many intervals are downtime.
GAP_INTERVALS = 3

# Every timestamp column, including second-grain ones (FeedHeader.timestamp,
# active_period): parquet has no seconds unit, so timestamp[s] would silently
# come back as [ms] -- one honest type beats a declared one readers don't get.
_TS_US = pa.timestamp("us", tz="UTC")

_ID_FIELDS = [
    pa.field("schema_version", pa.int16(), nullable=False),
    pa.field("feed_id", pa.string(), nullable=False),
    pa.field("mdb_feed_id", pa.string()),
]
_RUN_FIELDS = [
    pa.field("run_id", pa.string(), nullable=False),
    pa.field("first_seen", _TS_US, nullable=False),
    pa.field("last_seen", _TS_US, nullable=False),
    pa.field("first_feed_timestamp", _TS_US),
    pa.field("last_feed_timestamp", _TS_US),
    pa.field("poll_count", pa.int32(), nullable=False),
    pa.field("continues_from_prev_day", pa.bool_(), nullable=False),
    pa.field("continues_next_day", pa.bool_(), nullable=False),
    pa.field("spans_gap", pa.bool_(), nullable=False),
]

ALERT_TEXT_SCHEMA = pa.schema(
    _ID_FIELDS
    + [
        pa.field("alert_id", pa.string(), nullable=False),
        pa.field("field", pa.string(), nullable=False),
        pa.field("language", pa.string()),
        pa.field("text", pa.string(), nullable=False),
        pa.field("text_sha256", pa.string(), nullable=False),
        pa.field("translation_index", pa.int16(), nullable=False),
    ]
    + _RUN_FIELDS
)

_TRANSLATION = pa.struct([("text", pa.string()), ("language", pa.string())])
_TRIP = pa.struct(
    [
        ("trip_id", pa.string()),
        ("route_id", pa.string()),
        ("direction_id", pa.int32()),
        ("start_time", pa.string()),
        ("start_date", pa.string()),
        ("schedule_relationship", pa.string()),
    ]
)
_ENTITY_SELECTOR = pa.struct(
    [
        ("agency_id", pa.string()),
        ("route_id", pa.string()),
        ("route_type", pa.int32()),
        ("direction_id", pa.int32()),
        ("trip", _TRIP),
        ("stop_id", pa.string()),
    ]
)

ALERT_VERSIONS_SCHEMA = pa.schema(
    _ID_FIELDS
    + [pa.field("alert_id", pa.string(), nullable=False)]
    + _RUN_FIELDS
    + [
        pa.field("cause", pa.string()),
        pa.field("effect", pa.string()),
        pa.field("severity_level", pa.string()),
        pa.field("cause_detail", pa.list_(_TRANSLATION)),
        pa.field("effect_detail", pa.list_(_TRANSLATION)),
        pa.field(
            "active_period",
            pa.list_(pa.struct([("start", _TS_US), ("end", _TS_US)])),
        ),
        pa.field("informed_entity", pa.list_(_ENTITY_SELECTOR)),
        pa.field(
            "image",
            pa.list_(
                pa.struct(
                    [
                        ("url", pa.string()),
                        ("media_type", pa.string()),
                        ("language", pa.string()),
                    ]
                )
            ),
        ),
        pa.field("extensions_raw", pa.binary()),
        pa.field("version_sha256", pa.string(), nullable=False),
    ]
)

ALERT_PAYLOADS_SCHEMA = pa.schema(
    _ID_FIELDS
    + [
        pa.field("digest", pa.string(), nullable=False),
        pa.field("first_fetched_at", _TS_US, nullable=False),
        pa.field("last_fetched_at", _TS_US, nullable=False),
        pa.field("poll_count", pa.int32(), nullable=False),
        pa.field("parsed", pa.bool_(), nullable=False),
        # True when the payload's bytes aren't in this day's landing (a
        # duplicate/304 of the previous day's last payload) and its facts were
        # carried over from the previous day's alert_payloads row.
        pa.field("carried_from_prev_day", pa.bool_(), nullable=False),
        pa.field("feed_timestamp", _TS_US),
        pa.field("gtfs_realtime_version", pa.string()),
        pa.field("incrementality", pa.string()),
        pa.field("entity_count", pa.int32()),
        pa.field("alert_count", pa.int32()),
    ]
)

# Column descriptions, stored as each field's parquet metadata ("description")
# so a file describes itself to readers who don't have this repo, and rendered
# into docs/schemas/alert_text.md by schema_markdown().
_RUN_DESCRIPTIONS = {
    "schema_version": "Table schema version (also in the file's key-value metadata).",
    "feed_id": "Our feed name, e.g. massachusetts-bay-transportation-authority-mbta-alerts-1603.",
    "mdb_feed_id": "Mobility Database feed id from config, e.g. mdb-1603.",
    "alert_id": "GTFS-RT FeedEntity.id.",
    "run_id": "Stable id of the run, shared by its rows in every day partition. GROUP BY run_id to stitch a run across midnight.",
    "first_seen": "First poll (fetched_at) in this run, clipped to this day.",
    "last_seen": "Last poll in this run, clipped to this day.",
    "first_feed_timestamp": "FeedHeader.timestamp of the payload at first_seen.",
    "last_feed_timestamp": "FeedHeader.timestamp of the payload at last_seen.",
    "poll_count": "Parsed polls in this row (this day's slice of the run), including 304 and duplicate polls.",
    "continues_from_prev_day": "The run was already open at the end of the previous day.",
    "continues_next_day": "The run was still open at this day's last parsed poll.",
    "spans_gap": "A downtime or failure gap falls inside [first_seen, last_seen], or (for a run continuing from the previous day) between its previous-day last_seen and this first_seen.",
}
_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "alert_text": {
        **_RUN_DESCRIPTIONS,
        "field": "Proto field name of the TranslatedString: header_text, description_text, url, tts_header_text, tts_description_text, image_alternative_text, cause_detail, effect_detail.",
        "language": "Language tag exactly as published; null when absent or empty. Never defaulted to en.",
        "text": "Text byte-for-byte as published: no trimming, Unicode normalization or whitespace changes.",
        "text_sha256": "Hex SHA-256 of the UTF-8 text, for cheap equality joins.",
        "translation_index": "Position within translation[] at the run's first poll. Two entries with the same language in one field are separate runs, told apart by this.",
    },
    "alert_versions": {
        **_RUN_DESCRIPTIONS,
        "cause": "Alert.cause enum name; UNKNOWN_ENUM_<n> for a value these bindings don't know; null if unset.",
        "effect": "Alert.effect enum name; same rules as cause.",
        "severity_level": "Alert.severity_level enum name; same rules as cause.",
        "cause_detail": "Alert.cause_detail translations (also emitted as alert_text rows).",
        "effect_detail": "Alert.effect_detail translations (also emitted as alert_text rows).",
        "active_period": "Alert.active_period in published order. Some feeds (MBTA) roll end forward every minute on ongoing alerts, so each roll is a new version.",
        "informed_entity": "Alert.informed_entity in published order.",
        "image": "Alert.image (TranslatedImage.localized_image).",
        "extensions_raw": "Every field on the Alert these bindings don't know (unknown fields and extensions), serialized byte-for-byte; null if none.",
        "version_sha256": "SHA-256 over the version columns above; a change starts a new version run.",
    },
    "alert_payloads": {
        **{
            k: _RUN_DESCRIPTIONS[k]
            for k in ("schema_version", "feed_id", "mdb_feed_id")
        },
        "digest": "SHA-256 of the payload; joins to metadata.digest (see the poll_log view).",
        "first_fetched_at": "First poll on this day that served this payload.",
        "last_fetched_at": "Last poll on this day that served this payload.",
        "poll_count": "Polls on this day that served this payload, including 304s and duplicates.",
        "parsed": "False if the payload failed to parse; the row is kept so failures are visible.",
        "carried_from_prev_day": "The payload's bytes are only in the previous day's landing (a 304/duplicate of its last payload); these facts were carried from that day's row.",
        "feed_timestamp": "FeedHeader.timestamp.",
        "gtfs_realtime_version": "FeedHeader.gtfs_realtime_version.",
        "incrementality": "FeedHeader.incrementality enum name.",
        "entity_count": "FeedEntity count in the payload.",
        "alert_count": "FeedEntities carrying an alert (duplicate ids counted once).",
    },
}
_TABLE_DESCRIPTIONS = {
    "alert_text": "One row per run of one exact text for one (alert, TranslatedString field, language), clipped to the UTC day.",
    "alert_versions": "One row per run of one set of non-text alert metadata per alert, clipped to the UTC day. Text changes don't start a version; alert_text covers them.",
    "alert_payloads": "One row per distinct payload digest the day's polls referenced. Join to metadata on (feed, day, digest) for the per-poll log.",
}


def _describe(schema: pa.Schema, table: str) -> pa.Schema:
    return pa.schema(
        [f.with_metadata({"description": _DESCRIPTIONS[table][f.name]}) for f in schema]
    )


ALERT_TEXT_SCHEMA = _describe(ALERT_TEXT_SCHEMA, "alert_text")
ALERT_VERSIONS_SCHEMA = _describe(ALERT_VERSIONS_SCHEMA, "alert_versions")
ALERT_PAYLOADS_SCHEMA = _describe(ALERT_PAYLOADS_SCHEMA, "alert_payloads")

SCHEMAS: dict[str, pa.Schema] = {
    "alert_text": ALERT_TEXT_SCHEMA,
    "alert_versions": ALERT_VERSIONS_SCHEMA,
    "alert_payloads": ALERT_PAYLOADS_SCHEMA,
}


def schema_markdown() -> str:
    """docs/schemas/alert_text.md, generated from SCHEMAS (see the test that
    keeps the committed copy current)."""
    lines = [
        "# Alert translation tables",
        "",
        "<!-- Generated from analysis/alert_text.py by",
        "     `uv run python pipeline/alert_text.py --write-schema-doc`.",
        "     Don't edit by hand: tests/test_analysis_alert_text.py fails on drift. -->",
        "",
        f"Schema version **{SCHEMA_VERSION}**, stored as the `schema_version` column "
        "and in each file's parquet key-value metadata. Column descriptions are also "
        "stored in each file, as the fields' `description` metadata.",
        "",
        "Design: [docs/design/alert-translations.md](../design/alert-translations.md). "
        "Reader views (`poll_log`, `gaps`): "
        "[docs/design/alert-translations.sql](../design/alert-translations.sql).",
        "",
        "```",
        *(
            f"s3://rail-performance-archiver-hot/{t}/feed={{feed_id}}/year=YYYY/month=M/day=D/data.parquet"
            for t in TABLES
        ),
        "```",
        "",
        "Days are UTC days. Every timestamp is `timestamp[us, UTC]`.",
        "",
        "A **run** is a maximal stretch of consecutive successfully parsed polls in "
        "which a key had one value. A parsed poll with a different value, or without "
        "the key, ends it, so A→B→A is three runs and an alert that disappears and "
        "returns starts a new one. Failed polls and downtime don't end a run; they "
        "set `spans_gap`.",
        "",
        "**Not recoverable:** anything before 2026-06-08; any day with no cold "
        "tarball (those days have no rows, and aren't reconstructed from the "
        "flattened `alerts` parquet or the daily snapshots); lifecycle timestamps "
        "from MBTA's enhanced JSON feed, which isn't archived.",
    ]
    for table in TABLES:
        lines += [
            "",
            f"## `{table}`",
            "",
            _TABLE_DESCRIPTIONS[table],
            "",
            "| column | type | nullable | description |",
            "|---|---|---|---|",
        ]
        for f in SCHEMAS[table]:
            description = f.metadata[b"description"].decode().replace("|", "\\|")
            lines.append(
                f"| `{f.name}` | `{f.type}` | {'yes' if f.nullable else 'no'} | {description} |"
            )
    return "\n".join(lines) + "\n"


# Columns of alert_versions that make up a version (hashed into version_sha256).
_VERSION_COLUMNS = (
    "cause",
    "effect",
    "severity_level",
    "cause_detail",
    "effect_detail",
    "active_period",
    "informed_entity",
    "image",
    "extensions_raw",
)

# TranslatedString fields are discovered from the bindings' descriptor, so a
# bindings upgrade picks up new ones with no code change. The installed
# gtfs-realtime-bindings (2.0.0) predate these four spec fields, so when the
# descriptor lacks them they arrive as unknown fields and are decoded by number.
_TRANSLATED_STRING_FIELDS: tuple[tuple[str, int], ...] = tuple(
    (f.name, f.number)
    for f in Alert.DESCRIPTOR.fields
    if f.message_type is not None
    and f.message_type.full_name == TranslatedString.DESCRIPTOR.full_name
)
_KNOWN_NUMBERS = {f.number for f in Alert.DESCRIPTOR.fields}
_UNKNOWN_TRANSLATED_FIELDS = {
    number: name
    for number, name in (
        (16, "image_alternative_text"),
        (17, "cause_detail"),
        (18, "effect_detail"),
    )
    if number not in _KNOWN_NUMBERS
}
_IMAGE_FIELD_NUMBER = 15
_ENUM_FIELDS = (
    ("cause", 6, Alert.Cause),
    ("effect", 7, Alert.Effect),
    ("severity_level", 14, Alert.SeverityLevel),
)

_CARRIED = "carried"  # content-state / alert-state key for previous-day state
_WIRE_VARINT, _WIRE_LEN = 0, 2


# --- pass 1: reduce each distinct payload -----------------------------------


@dataclass(frozen=True)
class _AlertState:
    # (field, language, ordinal, translation_index, text). `ordinal` tells apart
    # repeated languages within one field (0 for the first "en", 1 for a second
    # "en", ...); it is part of the run key, translation_index is not, so a
    # feed reordering its translation[] doesn't restart every run.
    texts: tuple[tuple[str, str | None, int, int, str], ...]
    version: dict[str, Any] | None
    version_sha256: str | None


@dataclass
class _Payload:
    parsed: bool
    state: str | None = None  # content-state key; None iff not parsed
    feed_timestamp: int | None = None
    gtfs_realtime_version: str | None = None
    incrementality: str | None = None
    entity_count: int | None = None
    alert_count: int | None = None
    carried: bool = False


def _opt(msg, name: str):
    return getattr(msg, name) if msg.HasField(name) else None


def _unknown(msg) -> list[tuple[int, int, Any]]:
    return [
        (f.field_number, f.wire_type, f.data)
        for f in unknown_fields.UnknownFieldSet(msg)
    ]


def _wire_strings(data: bytes) -> dict[int, list[str]]:
    """Decode a message we have no binding for into {field_number: [strings]}."""
    holder = Empty()
    holder.ParseFromString(data)
    out: dict[int, list[str]] = {}
    for number, wire, value in _unknown(holder):
        if wire == _WIRE_LEN:
            out.setdefault(number, []).append(value.decode("utf-8", "replace"))
    return out


def _translations(ts: TranslatedString) -> list[tuple[str | None, str]]:
    """(language, text) per translation, in order. Absent and "" languages
    both become None, per the spec; text is kept byte-for-byte."""
    return [
        (tr.language if tr.HasField("language") and tr.language else None, tr.text)
        for tr in ts.translation
    ]


def _image(data: bytes) -> list[dict]:
    """TranslatedImage -> [{url, media_type, language}] (LocalizedImage = 1)."""
    images = []
    holder = Empty()
    holder.ParseFromString(data)
    for number, wire, value in _unknown(holder):
        if number == 1 and wire == _WIRE_LEN:
            fields = _wire_strings(value)
            images.append(
                {
                    "url": (fields.get(1) or [None])[0],
                    "media_type": (fields.get(2) or [None])[0],
                    "language": (fields.get(3) or [None])[0] or None,
                }
            )
    return images


def _trip(trip: TripDescriptor) -> dict:
    sr = _opt(trip, "schedule_relationship")
    return {
        "trip_id": _opt(trip, "trip_id"),
        "route_id": _opt(trip, "route_id"),
        "direction_id": _opt(trip, "direction_id"),
        "start_time": _opt(trip, "start_time"),
        "start_date": _opt(trip, "start_date"),
        "schedule_relationship": (
            TripDescriptor.ScheduleRelationship.Name(sr) if sr is not None else None
        ),
    }


def _reduce_alert(alert: Alert) -> _AlertState:
    unknown = _unknown(alert)

    translated: list[tuple[str, list[tuple[str | None, str]]]] = [
        (name, _translations(getattr(alert, name)))
        for name, _ in _TRANSLATED_STRING_FIELDS
        if alert.HasField(name)
    ]
    extra: dict[str, bytes] = {}
    image: list[dict] | None = None
    for number, wire, value in unknown:
        if wire != _WIRE_LEN:
            continue
        if number in _UNKNOWN_TRANSLATED_FIELDS:
            # A repeated occurrence of a singular message field merges, which
            # for TranslatedString means its translation lists concatenate.
            name = _UNKNOWN_TRANSLATED_FIELDS[number]
            extra[name] = extra.get(name, b"") + value
        elif number == _IMAGE_FIELD_NUMBER:
            image = (image or []) + _image(value)
    for name, data in extra.items():
        ts = TranslatedString()
        ts.ParseFromString(data)
        translated.append((name, _translations(ts)))

    texts = []
    for name, translations in translated:
        seen: dict[str | None, int] = {}
        for index, (language, text) in enumerate(translations):
            ordinal = seen.get(language, 0)
            seen[language] = ordinal + 1
            texts.append((name, language, ordinal, index, text))

    version: dict[str, Any] = {}
    for column, number, enum in _ENUM_FIELDS:
        if alert.HasField(column):
            version[column] = enum.Name(getattr(alert, column))
        else:
            # proto2 enums are closed: a value these bindings don't know is
            # parked in the unknown fields rather than set, so fish it out
            # there instead of dropping it.
            raw = next(
                (v for n, w, v in unknown if n == number and w == _WIRE_VARINT),
                None,
            )
            version[column] = f"UNKNOWN_ENUM_{raw}" if raw is not None else None
    by_name = dict(translated)
    for column in ("cause_detail", "effect_detail"):
        version[column] = (
            [{"text": t, "language": lang} for lang, t in by_name[column]]
            if column in by_name
            else None
        )
    version["active_period"] = [
        {"start": _utc(_opt(p, "start")), "end": _utc(_opt(p, "end"))}
        for p in alert.active_period
    ]
    version["informed_entity"] = [
        {
            "agency_id": _opt(e, "agency_id"),
            "route_id": _opt(e, "route_id"),
            "route_type": _opt(e, "route_type"),
            "direction_id": _opt(e, "direction_id"),
            "trip": _trip(e.trip) if e.HasField("trip") else None,
            "stop_id": _opt(e, "stop_id"),
        }
        for e in alert.informed_entity
    ]
    version["image"] = image
    if unknown:
        # Everything the bindings don't know, byte-for-byte: clear the known
        # fields on a copy and what serializes is exactly the unknown set.
        rest = Alert()
        rest.CopyFrom(alert)
        for fd, _ in rest.ListFields():
            rest.ClearField(fd.name)
        version["extensions_raw"] = rest.SerializeToString()
    else:
        version["extensions_raw"] = None

    return _AlertState(tuple(texts), version, _version_sha256(version))


def _version_sha256(version: dict[str, Any]) -> str:
    def default(value):
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, datetime):
            return int(value.timestamp())
        raise TypeError(type(value))

    canonical = json.dumps(
        {c: version.get(c) for c in _VERSION_COLUMNS},
        sort_keys=True,
        separators=(",", ":"),
        default=default,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class _Reducer:
    """Pass 1 state: interned alert bodies and whole-payload alert contents."""

    def __init__(self) -> None:
        self.alerts: dict[str, _AlertState] = {}  # alert-body sha -> state
        self.states: dict[str, dict[str, str]] = {}  # content sha -> {id: body sha}
        self.duplicate_ids = 0

    def reduce(self, message) -> _Payload:
        header = message.header
        incrementality = _opt(header, "incrementality")
        content = hashlib.sha256()
        alerts: dict[str, str] = {}
        for entity in message.entity:
            if not entity.HasField("alert"):
                continue
            if entity.id in alerts:
                # Two entities with one id in one payload: keep the first,
                # count the rest, rather than letting order decide silently.
                self.duplicate_ids += 1
                continue
            body = entity.alert.SerializeToString(deterministic=True)
            body_sha = hashlib.sha256(body).hexdigest()
            if body_sha not in self.alerts:
                self.alerts[body_sha] = _reduce_alert(entity.alert)
            alerts[entity.id] = body_sha
            content.update(entity.id.encode() + b"\x00" + body_sha.encode() + b"\x00")
        state = content.hexdigest()
        self.states.setdefault(state, alerts)
        return _Payload(
            parsed=True,
            state=state,
            feed_timestamp=_opt(header, "timestamp"),
            gtfs_realtime_version=_opt(header, "gtfs_realtime_version"),
            incrementality=(
                FeedHeader.Incrementality.Name(incrementality)
                if incrementality is not None
                else None
            ),
            entity_count=len(message.entity),
            alert_count=len(alerts),
        )


# --- previous-day context ---------------------------------------------------


@dataclass
class PrevContext:
    """What day D needs from day D-1's output: the runs still open at its end
    (which ARE the content of its last parsed poll) and that poll's payload."""

    text_runs: list[dict]  # alert_text rows with continues_next_day
    version_runs: list[dict]  # alert_versions rows with continues_next_day
    last_payload: dict | None  # alert_payloads row of the last parsed digest


def partition_path(base: str, table: str, feed_id: str, day: date) -> str:
    return (
        f"{base}/{table}/feed={feed_id}/year={day.year}/month={day.month}"
        f"/day={day.day}/data.parquet"
    )


def load_prev_context(
    roots: list[Path | str], feed_id: str, day: date
) -> PrevContext | None:
    """Read day-1's three partitions from the first root that has them.

    `roots` are curated roots (local paths or s3:// URIs, via curated_fs), so
    the nightly can look at local curated first and fall back to the hot
    bucket. alert_payloads is the marker: it's always written, even for a day
    with no alerts, so its absence means day-1 was never built.
    """
    prev = day - timedelta(days=1)
    for root in roots:
        fs, base = curated_fs(root)
        paths = {t: partition_path(base, t, feed_id, prev) for t in TABLES}
        if fs.get_file_info(paths["alert_payloads"]).type == FileType.NotFound:
            continue
        tables = {
            t: pq.read_table(p, filesystem=fs, partitioning=None).to_pylist()
            if fs.get_file_info(p).type != FileType.NotFound
            else []
            for t, p in paths.items()
        }
        parsed = [r for r in tables["alert_payloads"] if r["parsed"]]
        return PrevContext(
            text_runs=[r for r in tables["alert_text"] if r["continues_next_day"]],
            version_runs=[
                r for r in tables["alert_versions"] if r["continues_next_day"]
            ],
            last_payload=max(parsed, key=lambda r: r["last_fetched_at"])
            if parsed
            else None,
        )
    return None


def _carried_state(
    prev: PrevContext,
) -> tuple[dict[str, _AlertState], dict[tuple, dict]]:
    """Rebuild the previous day's last content from its open runs.

    Returns ({alert_id: _AlertState}, {text run key: its alert_text row}). The
    ordinal isn't stored, so it's re-derived the way _reduce_alert assigns it:
    by translation_index order within (field, language).
    """
    texts: dict[str, list[dict]] = {}
    for row in prev.text_runs:
        texts.setdefault(row["alert_id"], []).append(row)
    versions = {row["alert_id"]: row for row in prev.version_runs}
    alerts: dict[str, _AlertState] = {}
    rows_by_key: dict[tuple, dict] = {}
    for alert_id in texts.keys() | versions.keys():
        seen: dict[tuple[str, str | None], int] = {}
        reduced = []
        for r in sorted(texts.get(alert_id, []), key=lambda r: r["translation_index"]):
            ordinal = seen.get((r["field"], r["language"]), 0)
            seen[(r["field"], r["language"])] = ordinal + 1
            reduced.append(
                (r["field"], r["language"], ordinal, r["translation_index"], r["text"])
            )
            rows_by_key[(alert_id, r["field"], r["language"], ordinal)] = r
        vrow = versions.get(alert_id)
        alerts[alert_id] = _AlertState(
            tuple(reduced),
            {c: vrow[c] for c in _VERSION_COLUMNS} if vrow else None,
            vrow["version_sha256"] if vrow else None,
        )
    return alerts, rows_by_key


# --- pass 2: fold runs over the poll log ------------------------------------


@dataclass
class _Run:
    run_id: str
    value: Any  # (text, translation_index) or (version_sha256, version)
    continues_from_prev_day: bool
    prev_last_seen_us: int | None = None  # carried runs: D-1's last_seen
    # Parsed polls before this run's first observation. Every open run is
    # observed by every parsed poll (a poll that doesn't observe it closes
    # it), so poll_count and last_seen fall out of the global clock at close
    # time -- no per-poll work per open run.
    start_n: int = 0
    first_seen_us: int | None = None
    first_feed_ts: int | None = None


@dataclass
class _Clock:
    """The parsed polls folded so far: how many, and the latest one's times."""

    n: int = 0
    last_us: int | None = None
    last_feed_ts: int | None = None


def _dt_us(value) -> int:
    if isinstance(value, datetime):
        return to_us(value.timestamp())
    return int(value)


def _feed_ts(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def _utc(seconds: int | None) -> datetime | None:
    """Epoch seconds -> aware datetime; None for values no timestamp can hold
    (feeds do send e.g. 2**64-1 as "no end"). The raw value is still in the
    landing payload."""
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _run_id(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _read_polls(
    source: Source, feed_name: str, day: date
) -> list[tuple[int, str | None]]:
    """(fetched_at_us, digest or None) per metadata row, in time order.

    Parsed by the rollup's own parse_metadata_lines and timed by to_us, so these
    polls are exactly the rows of the day's `metadata` table: a line the rollup
    skips is skipped here, a day the rollup fails on (too many unusable lines)
    fails here, and fetched_at joins to `metadata.timestamp` on equality.
    """
    rows, _ = parse_metadata_lines(source.read_metadata(feed_name, day))
    polls = [(to_us(row["timestamp"]), row.get("digest")) for row in rows]
    polls.sort(key=lambda p: p[0])
    return polls


def build_alert_tables(
    feed: Feed,
    day: date,
    source: Source,
    prev: PrevContext | None,
    *,
    mdb_feed_id: str | None = None,
    poll_interval_seconds: int | None = None,
) -> dict[str, pa.Table]:
    """Build {alert_text, alert_versions, alert_payloads} for one (feed, day)."""
    interval = (
        poll_interval_seconds
        or feed.poll_interval_seconds
        or DEFAULT_POLL_INTERVAL_SECONDS
    )
    gap_us = GAP_INTERVALS * interval * 1_000_000

    # Pass 1: decode each distinct stored payload once.
    reducer = _Reducer()
    payloads: dict[str, _Payload] = {}
    stored_at: dict[str, int] = {}  # digest -> fetched_at, for the no-digest fallback
    digest_ts = digest_timestamps(source, feed.name, day)
    for name, blob in source.iter_bins(feed.name, day):
        for payload, fetched_at in iter_payloads(name, blob, digest_ts):
            digest = hashlib.sha256(payload).hexdigest()
            stored_at.setdefault(digest, fetched_at)
            if digest in payloads:
                continue
            try:
                payloads[digest] = reducer.reduce(feed.parser.parse(payload))
            except ParseFailure:
                payloads[digest] = _Payload(parsed=False)

    # The previous day's last payload, if today's polls still reference it.
    carried_rows: dict[tuple, dict] = {}
    if prev is not None:
        carried_alerts, carried_rows = _carried_state(prev)
        for alert_id, state in carried_alerts.items():
            reducer.alerts[f"{_CARRIED}:{alert_id}"] = state
        reducer.states[_CARRIED] = {a: f"{_CARRIED}:{a}" for a in carried_alerts}
        last = prev.last_payload
        if last is not None and last["digest"] not in payloads:
            payloads[last["digest"]] = _Payload(
                parsed=True,
                state=_CARRIED,
                feed_timestamp=_feed_ts(last["feed_timestamp"]),
                gtfs_realtime_version=last["gtfs_realtime_version"],
                incrementality=last["incrementality"],
                entity_count=last["entity_count"],
                alert_count=last["alert_count"],
                carried=True,
            )

    polls = _read_polls(source, feed.name, day)
    if payloads and not any(digest for _, digest in polls):
        # Metadata from before the poller recorded digests: every stored
        # payload was one poll, timed by its frame/window.
        logger.warning(
            "alert_text %s %s: metadata has no digests; one poll per stored payload",
            feed.name,
            day,
        )
        polls = sorted((to_us(ts), d) for d, ts in stored_at.items())

    # Pass 2: fold. Open runs are grouped by alert so a content change only
    # touches the alerts whose body changed (MBTA rolls active_period.end on
    # delay alerts every minute, so nearly every payload is a new content
    # state, but it's one alert's body that moved, not all 200).
    open_text: dict[str, dict[tuple, _Run]] = {}
    open_versions: dict[str, _Run] = {}
    text_rows: list[dict] = []
    version_rows: list[dict] = []
    references: dict[str, list[int]] = {}  # digest -> [first_us, last_us, count]
    failures: list[int] = []
    unresolved = 0
    clock = _Clock()

    if prev is not None:
        for key, row in carried_rows.items():
            open_text.setdefault(key[0], {})[key] = _Run(
                run_id=row["run_id"],
                value=(row["text"], row["translation_index"]),
                continues_from_prev_day=True,
                prev_last_seen_us=_dt_us(row["last_seen"]),
            )
        for row in prev.version_runs:
            open_versions[row["alert_id"]] = _Run(
                run_id=row["run_id"],
                value=(row["version_sha256"], {c: row[c] for c in _VERSION_COLUMNS}),
                continues_from_prev_day=True,
                prev_last_seen_us=_dt_us(row["last_seen"]),
            )

    def close(run: _Run, sink: list, extra: dict, *, still_open: bool) -> None:
        poll_count = clock.n - run.start_n
        if poll_count == 0:
            return  # carried in, but never observed today: D-1's row covers it
        sink.append(
            {
                **extra,
                "run_id": run.run_id,
                "first_seen": run.first_seen_us,
                "last_seen": clock.last_us,
                "first_feed_timestamp": _utc(run.first_feed_ts),
                "last_feed_timestamp": _utc(clock.last_feed_ts),
                "poll_count": poll_count,
                "continues_from_prev_day": run.continues_from_prev_day,
                "continues_next_day": still_open,
                "_prev_last_seen_us": run.prev_last_seen_us,
            }
        )

    def text_extra(key: tuple, run: _Run) -> dict:
        alert_id, field_, language, _ = key
        text, tidx = run.value
        return {
            "alert_id": alert_id,
            "field": field_,
            "language": language,
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "translation_index": tidx,
        }

    def version_extra(alert_id: str, run: _Run) -> dict:
        sha, version = run.value
        return {"alert_id": alert_id, **version, "version_sha256": sha}

    def new_run(run_id: str, value, ts_us: int, feed_ts: int | None) -> _Run:
        return _Run(
            run_id=run_id,
            value=value,
            continues_from_prev_day=False,
            start_n=clock.n,
            first_seen_us=ts_us,
            first_feed_ts=feed_ts,
        )

    def diff_alert(
        alert_id: str, state: _AlertState | None, ts_us: int, feed_ts: int | None
    ) -> None:
        """Close/open alert_id's runs for its new body (None = absent)."""
        runs = open_text.pop(alert_id, {})
        texts = (
            {
                (alert_id, field_, lang, ordinal): (text, tidx)
                for field_, lang, ordinal, tidx, text in state.texts
            }
            if state is not None
            else {}
        )
        for key in [
            k for k, r in runs.items() if texts.get(k, (None,))[0] != r.value[0]
        ]:
            run = runs.pop(key)
            close(run, text_rows, text_extra(key, run), still_open=False)
        for key, (text, tidx) in texts.items():
            if key not in runs:
                runs[key] = new_run(
                    _run_id(feed.name, *key, text, ts_us), (text, tidx), ts_us, feed_ts
                )
        if runs:
            open_text[alert_id] = runs

        sha = state.version_sha256 if state is not None else None
        run = open_versions.get(alert_id)
        if run is not None and run.value[0] != sha:
            close(
                open_versions.pop(alert_id),
                version_rows,
                version_extra(alert_id, run),
                still_open=False,
            )
            run = None
        if run is None and sha is not None:
            open_versions[alert_id] = new_run(
                _run_id(feed.name, alert_id, sha, ts_us),
                (sha, state.version),
                ts_us,
                feed_ts,
            )

    current = _CARRIED if prev is not None else None
    bodies: dict[str, str] = reducer.states.get(current, {}) if current else {}
    for ts_us, digest in polls:
        payload = payloads.get(digest) if digest else None
        if payload is None:
            failures.append(ts_us)
            unresolved += bool(digest)
            continue
        ref = references.setdefault(digest, [ts_us, ts_us, 0])
        ref[1], ref[2] = ts_us, ref[2] + 1
        if not payload.parsed:
            failures.append(ts_us)
            continue

        if payload.state != current:
            new_bodies = reducer.states[payload.state]
            for alert_id in bodies.keys() | new_bodies.keys():
                old, new = bodies.get(alert_id), new_bodies.get(alert_id)
                if old != new:
                    diff_alert(
                        alert_id,
                        reducer.alerts[new] if new is not None else None,
                        ts_us,
                        payload.feed_timestamp,
                    )
            current, bodies = payload.state, new_bodies
        if clock.n == 0:
            # Carried runs that survived today's first parsed poll start here.
            for run in (
                *(r for runs in open_text.values() for r in runs.values()),
                *open_versions.values(),
            ):
                if run.first_seen_us is None:
                    run.first_seen_us = ts_us
                    run.first_feed_ts = payload.feed_timestamp
        clock.n += 1
        clock.last_us = ts_us
        clock.last_feed_ts = payload.feed_timestamp

    for runs in open_text.values():
        for key, run in runs.items():
            close(run, text_rows, text_extra(key, run), still_open=True)
    for alert_id, run in open_versions.items():
        close(run, version_rows, version_extra(alert_id, run), still_open=True)

    if unresolved:
        logger.warning(
            "alert_text %s %s: %d poll(s) reference a digest with no stored or "
            "carried payload",
            feed.name,
            day,
            unresolved,
        )
    if reducer.duplicate_ids:
        logger.warning(
            "alert_text %s %s: %d duplicate alert entity id(s) ignored",
            feed.name,
            day,
            reducer.duplicate_ids,
        )

    gaps = _gaps(polls, failures, gap_us)
    for row in (*text_rows, *version_rows):
        prev_last = row.pop("_prev_last_seen_us")
        row["spans_gap"] = any(
            row["first_seen"] <= start and end <= row["last_seen"]
            for start, end in gaps
        ) or (
            # A carried run also spans any downtime between D-1's last
            # observation and today's first, which neither day's clipped
            # window contains on its own.
            prev_last is not None and row["first_seen"] - prev_last > gap_us
        )

    ids = {
        "schema_version": SCHEMA_VERSION,
        "feed_id": feed.name,
        "mdb_feed_id": mdb_feed_id,
    }
    payload_rows = [
        {
            **ids,
            "digest": digest,
            "first_fetched_at": first_us,
            "last_fetched_at": last_us,
            "poll_count": count,
            "parsed": payloads[digest].parsed,
            "carried_from_prev_day": payloads[digest].carried,
            "feed_timestamp": _utc(payloads[digest].feed_timestamp),
            "gtfs_realtime_version": payloads[digest].gtfs_realtime_version,
            "incrementality": payloads[digest].incrementality,
            "entity_count": payloads[digest].entity_count,
            "alert_count": payloads[digest].alert_count,
        }
        for digest, (first_us, last_us, count) in sorted(
            references.items(), key=lambda kv: kv[1][0]
        )
    ]
    text_rows.sort(key=lambda r: (r["alert_id"], r["field"], r["first_seen"]))
    version_rows.sort(key=lambda r: (r["alert_id"], r["first_seen"]))
    return {
        "alert_text": _table([{**ids, **r} for r in text_rows], ALERT_TEXT_SCHEMA),
        "alert_versions": _table(
            [{**ids, **r} for r in version_rows], ALERT_VERSIONS_SCHEMA
        ),
        "alert_payloads": _table(payload_rows, ALERT_PAYLOADS_SCHEMA),
    }


def _gaps(
    polls: list[tuple[int, str | None]], failures: list[int], gap_us: int
) -> list[tuple[int, int]]:
    """Downtime gaps (consecutive rows > gap_us apart, as (prev, next)) and
    failure gaps (maximal stretches of failed rows, as (first, last))."""
    gaps = [(a, b) for (a, _), (b, _) in zip(polls, polls[1:]) if b - a > gap_us]
    failed = set(failures)
    stretch: list[int] = []
    for ts_us, _ in polls:
        if ts_us in failed:
            stretch.append(ts_us)
        elif stretch:
            gaps.append((stretch[0], stretch[-1]))
            stretch = []
    if stretch:
        gaps.append((stretch[0], stretch[-1]))
    return gaps


def _table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    table = pa.Table.from_pylist(rows, schema=schema)
    return table.replace_schema_metadata({"schema_version": str(SCHEMA_VERSION)})


def write_alert_tables(
    tables: dict[str, pa.Table], curated_dir: Path, feed_id: str, day: date
) -> list[Path]:
    """Write each table to its day partition (tmp + rename); returns the paths.

    Empty tables are written too: an empty alert_payloads is how the next day
    tells "built, no polls" from "never built" (see load_prev_context).
    """
    written = []
    for name, table in tables.items():
        out = Path(partition_path(str(curated_dir), name, feed_id, day))
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        pq.write_table(
            table,
            tmp,
            compression="zstd",
            use_dictionary=[
                c
                for c in ("feed_id", "mdb_feed_id", "alert_id", "field", "language")
                if c in table.column_names
            ],
        )
        tmp.rename(out)
        written.append(out)
    return written
