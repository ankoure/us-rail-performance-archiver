import hashlib
import json
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from google.transit.gtfs_realtime_pb2 import Alert, FeedHeader, FeedMessage

from analysis.alert_text import (
    ALERT_PAYLOADS_SCHEMA,
    ALERT_TEXT_SCHEMA,
    ALERT_VERSIONS_SCHEMA,
    build_alert_tables,
    load_prev_context,
    schema_markdown,
    write_alert_tables,
)
from archiver.feed import Feed
from archiver.parser import Parser
from archiver.source import LocalSource

DAY = date(2026, 9, 20)
NEXT_DAY = date(2026, 9, 21)
# 2026-09-20T00:00:00Z; polls are placed at T0 + offset seconds.
T0 = 1789862400
T1 = T0 + 86400


def _feed(name: str = "test-alerts", interval: int = 30) -> Feed:
    return Feed(
        name=name,
        path="/irrelevant",
        parser=Parser.from_name("protobuf"),
        decoder=None,
        agency_id="TEST_AGENCY",
        poll_interval_seconds=interval,
    )


def _alert(
    header: dict[str, str] | list[tuple[str | None, str]],
    *,
    effect: int | None = None,
    extra: bytes = b"",
) -> bytes:
    """Serialized Alert. `header` is {language: text} or [(language, text)];
    a None language leaves the field unset. `extra` is appended raw wire bytes
    (fields these bindings don't know)."""
    alert = Alert()
    items = header.items() if isinstance(header, dict) else header
    for language, text in items:
        tr = alert.header_text.translation.add()
        tr.text = text
        if language is not None:
            tr.language = language
    if effect is not None:
        alert.effect = effect
    return alert.SerializeToString() + extra


def _message(header_ts: int, alerts: dict[str, bytes]) -> bytes:
    feed = FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = FeedHeader.FULL_DATASET
    feed.header.timestamp = header_ts
    for alert_id, body in alerts.items():
        entity = feed.entity.add()
        entity.id = alert_id
        entity.alert.MergeFromString(body)
    return feed.SerializeToString()


def _land(root, feed_name: str, day: date, polls: list) -> None:
    """Write a content-addressed landing day: each distinct payload once as
    {sha256}.bin, and one metadata row per poll. A poll is (offset, payload)
    for a stored/duplicate response, (offset, "error") for a transport error
    (no digest), or (offset, ("digest", hexdigest)) to reference a payload
    stored on another day."""
    day_start = T0 if day == DAY else T1
    part = f"year={day.year}/month={day.month}/day={day.day}"
    raw_dir = root / feed_name / "raw" / part
    meta_dir = root / feed_name / "metadata" / part
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for offset, payload in polls:
        ts = day_start + offset + 0.25  # real metadata timestamps are floats
        if payload == "error":
            rows.append({"timestamp": ts, "error_type": "ConnectError"})
            continue
        if isinstance(payload, tuple):
            rows.append({"timestamp": ts, "digest": payload[1]})
            continue
        digest = hashlib.sha256(payload).hexdigest()
        (raw_dir / f"{digest}.bin").write_bytes(payload)
        rows.append({"timestamp": ts, "digest": digest})
    with (meta_dir / "data.jsonl").open("a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _build(tmp_path, day=DAY, prev=None, feed=None):
    feed = feed or _feed()
    tables = build_alert_tables(
        feed, day, LocalSource(tmp_path / "landing"), prev, mdb_feed_id="mdb-1"
    )
    return {name: t.to_pylist() for name, t in tables.items()}, tables


def _us(day_start: int, offset: int) -> int:
    return (day_start + offset) * 1_000_000 + 250_000


def _ts(row, col):
    return int(row[col].timestamp() * 1_000_000)


def test_flip_back_is_three_runs_even_though_a_is_stored_once(tmp_path):
    a = _message(T0, {"1": _alert({"en": "A"})})
    b = _message(T0 + 30, {"1": _alert({"en": "B"})})
    # The third poll re-serves A byte-for-byte: one stored bin, two polls.
    _land(tmp_path / "landing", "test-alerts", DAY, [(0, a), (30, b), (60, a)])

    rows, _ = _build(tmp_path)

    text = sorted(rows["alert_text"], key=lambda r: r["first_seen"])
    assert [r["text"] for r in text] == ["A", "B", "A"]
    assert [r["poll_count"] for r in text] == [1, 1, 1]
    assert len({r["run_id"] for r in text}) == 3
    assert [r["continues_next_day"] for r in text] == [False, False, True]
    assert (tmp_path / "landing" / "test-alerts" / "raw").exists()
    assert len(list((tmp_path / "landing").rglob("*.bin"))) == 2


def test_duplicate_304_and_header_only_changes_extend_one_run(tmp_path):
    first = _message(T0, {"1": _alert({"en": "A"})})
    # Same alert content, new header timestamp: a new digest, same run.
    second = _message(T0 + 60, {"1": _alert({"en": "A"})})
    digest_first = hashlib.sha256(first).hexdigest()
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, first),
            (30, ("digest", digest_first)),  # duplicate / 304 of `first`
            (60, second),
            (90, _message(T0 + 90, {"1": _alert({"en": "B"})})),
        ],
    )

    rows, _ = _build(tmp_path)

    a, b = sorted(rows["alert_text"], key=lambda r: r["first_seen"])
    assert (a["text"], a["poll_count"]) == ("A", 3)
    assert _ts(a, "first_seen") == _us(T0, 0)
    assert _ts(a, "last_seen") == _us(T0, 60)
    assert int(a["first_feed_timestamp"].timestamp()) == T0
    assert int(a["last_feed_timestamp"].timestamp()) == T0 + 60
    assert a["continues_next_day"] is False
    assert (b["text"], b["poll_count"], b["continues_next_day"]) == ("B", 1, True)

    payloads = {r["digest"]: r for r in rows["alert_payloads"]}
    assert payloads[digest_first]["poll_count"] == 2
    assert payloads[digest_first]["alert_count"] == 1
    assert payloads[digest_first]["incrementality"] == "FULL_DATASET"


def test_errors_do_not_end_a_run_but_mark_it_as_spanning_a_gap(tmp_path):
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, _message(T0, {"1": _alert({"en": "A"})})),
            (30, "error"),
            (60, _message(T0 + 60, {"1": _alert({"en": "A"})})),
            (
                90,
                _message(T0 + 90, {"1": _alert({"en": "A"}), "2": _alert({"en": "X"})}),
            ),
        ],
    )

    rows, _ = _build(tmp_path)

    by_alert = {r["alert_id"]: r for r in rows["alert_text"]}
    assert by_alert["1"]["poll_count"] == 3
    assert by_alert["1"]["spans_gap"] is True
    # Alert 2 starts after the error, so no gap falls inside its window.
    assert by_alert["2"]["spans_gap"] is False


def test_downtime_longer_than_three_intervals_is_a_gap(tmp_path):
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, _message(T0, {"1": _alert({"en": "A"})})),
            (30, _message(T0 + 30, {"1": _alert({"en": "A"})})),
            (1000, _message(T0 + 1000, {"1": _alert({"en": "A"})})),
        ],
    )

    rows, _ = _build(tmp_path)

    (row,) = rows["alert_text"]
    assert row["poll_count"] == 3
    assert row["spans_gap"] is True


def test_alert_that_disappears_and_returns_gets_a_new_run(tmp_path):
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, _message(T0, {"1": _alert({"en": "A"})})),
            (30, _message(T0 + 30, {})),  # parsed, alert absent
            (60, _message(T0 + 60, {"1": _alert({"en": "A"})})),
        ],
    )

    rows, _ = _build(tmp_path)

    runs = sorted(rows["alert_text"], key=lambda r: r["first_seen"])
    assert [r["poll_count"] for r in runs] == [1, 1]
    assert runs[0]["run_id"] != runs[1]["run_id"]
    assert len(rows["alert_versions"]) == 2


def test_languages_run_independently_and_blank_languages_are_null(tmp_path):
    first = [("en", "Delay"), ("es-419", "Retraso"), ("", "blank"), (None, "unset")]
    second = [("en", "Delay"), ("es-419", "Demora"), ("", "blank"), (None, "unset")]
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, _message(T0, {"1": _alert(first)})),
            (30, _message(T0 + 30, {"1": _alert(second)})),
        ],
    )

    rows, _ = _build(tmp_path)

    by_lang: dict = {}
    for r in rows["alert_text"]:
        by_lang.setdefault(r["language"], []).append(r)
    assert [r["poll_count"] for r in by_lang["en"]] == [2]
    assert sorted(r["text"] for r in by_lang["es-419"]) == ["Demora", "Retraso"]
    # "" and absent both fold to null, and stay two keys (not merged).
    nulls = sorted(by_lang[None], key=lambda r: r["translation_index"])
    assert [(r["text"], r["translation_index"]) for r in nulls] == [
        ("blank", 2),
        ("unset", 3),
    ]
    assert all(r["poll_count"] == 2 for r in nulls)


def test_metadata_change_is_a_new_version_but_not_a_new_text_run(tmp_path):
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, _message(T0, {"1": _alert({"en": "A"}, effect=Alert.DETOUR)})),
            (
                30,
                _message(T0 + 30, {"1": _alert({"en": "A"}, effect=Alert.NO_SERVICE)}),
            ),
        ],
    )

    rows, _ = _build(tmp_path)

    assert len(rows["alert_text"]) == 1
    versions = sorted(rows["alert_versions"], key=lambda r: r["first_seen"])
    assert [v["effect"] for v in versions] == ["DETOUR", "NO_SERVICE"]
    assert versions[0]["version_sha256"] != versions[1]["version_sha256"]


def test_unknown_enum_and_spec_fields_newer_than_the_bindings(tmp_path):
    cause_detail = b"\x0a\x0e\x0a\x08Flooding\x12\x02en"  # TranslatedString
    extra = (
        b"\x30\x63"  # cause (6) = 99, not in this Cause enum
        + b"\x8a\x01"  # field 17 (cause_detail), length-delimited
        + bytes([len(cause_detail)])
        + cause_detail
    )
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [(0, _message(T0, {"1": _alert({"en": "A"}, extra=extra)}))],
    )

    rows, _ = _build(tmp_path)

    (version,) = rows["alert_versions"]
    assert version["cause"] == "UNKNOWN_ENUM_99"
    assert version["cause_detail"] == [{"text": "Flooding", "language": "en"}]
    assert version["extensions_raw"]
    fields = {(r["field"], r["text"]) for r in rows["alert_text"]}
    assert ("cause_detail", "Flooding") in fields


def test_unparseable_payload_is_kept_as_a_payload_row_and_is_not_evidence(tmp_path):
    garbage = b"\xff\xff\xff not a protobuf"
    _land(
        tmp_path / "landing",
        "test-alerts",
        DAY,
        [
            (0, _message(T0, {"1": _alert({"en": "A"})})),
            (30, garbage),
            (60, _message(T0 + 60, {"1": _alert({"en": "A"})})),
        ],
    )

    rows, _ = _build(tmp_path)

    (row,) = rows["alert_text"]
    assert row["poll_count"] == 2 and row["spans_gap"] is True
    bad = next(
        r
        for r in rows["alert_payloads"]
        if r["digest"] == hashlib.sha256(garbage).hexdigest()
    )
    assert bad["parsed"] is False and bad["feed_timestamp"] is None


def test_runs_carry_across_midnight_including_a_payload_stored_yesterday(tmp_path):
    feed = _feed()
    last_yesterday = _message(T0 + 86370, {"1": _alert({"en": "A"})})
    _land(
        tmp_path / "landing",
        feed.name,
        DAY,
        [(0, _message(T0, {"1": _alert({"en": "A"})})), (86370, last_yesterday)],
    )
    _, day1 = _build(tmp_path, DAY, feed=feed)
    write_alert_tables(day1, tmp_path / "curated", feed.name, DAY)

    # Today opens with a duplicate of yesterday's last payload -- its bytes
    # are only in yesterday's landing -- then new bytes with the same content.
    _land(
        tmp_path / "landing",
        feed.name,
        NEXT_DAY,
        [
            (0, ("digest", hashlib.sha256(last_yesterday).hexdigest())),
            (30, _message(T1 + 30, {"1": _alert({"en": "A"})})),
            (60, _message(T1 + 60, {"1": _alert({"en": "B"})})),
        ],
    )
    prev = load_prev_context([tmp_path / "curated"], feed.name, NEXT_DAY)
    assert prev is not None
    rows, _ = _build(tmp_path, NEXT_DAY, prev=prev, feed=feed)

    (yesterday,) = day1["alert_text"].to_pylist()
    assert yesterday["continues_next_day"] is True
    a, b = sorted(rows["alert_text"], key=lambda r: r["first_seen"])
    assert a["run_id"] == yesterday["run_id"]
    assert a["continues_from_prev_day"] is True
    assert a["poll_count"] == 2
    assert _ts(a, "first_seen") == _us(T1, 0)
    assert b["continues_from_prev_day"] is False
    carried = next(r for r in rows["alert_payloads"] if r["carried_from_prev_day"])
    assert carried["digest"] == hashlib.sha256(last_yesterday).hexdigest()
    assert carried["alert_count"] == 1
    (version,) = [v for v in rows["alert_versions"] if v["continues_from_prev_day"]]
    assert version["run_id"] == day1["alert_versions"].to_pylist()[0]["run_id"]


def test_missing_previous_day_starts_fresh(tmp_path):
    _land(
        tmp_path / "landing",
        "test-alerts",
        NEXT_DAY,
        [(0, _message(T1, {"1": _alert({"en": "A"})}))],
    )
    assert load_prev_context([tmp_path / "curated"], "test-alerts", NEXT_DAY) is None

    rows, _ = _build(tmp_path, NEXT_DAY)

    (row,) = rows["alert_text"]
    assert row["continues_from_prev_day"] is False


def test_written_files_carry_explicit_schemas_even_when_empty(tmp_path):
    _land(tmp_path / "landing", "test-alerts", DAY, [(0, "error")])
    _, tables = _build(tmp_path)

    paths = write_alert_tables(tables, tmp_path / "curated", "test-alerts", DAY)

    expected = {
        "alert_text": ALERT_TEXT_SCHEMA,
        "alert_versions": ALERT_VERSIONS_SCHEMA,
        "alert_payloads": ALERT_PAYLOADS_SCHEMA,
    }
    for path in paths:
        table_name = path.parts[-6]
        written = pq.read_table(path, partitioning=None)
        assert written.num_rows == 0
        assert written.schema.remove_metadata() == expected[table_name]
        assert pq.read_schema(path).metadata[b"schema_version"] == b"1"


def test_schema_doc_is_current():
    doc = Path(__file__).resolve().parent.parent / "docs/schemas/alert_text.md"
    assert doc.read_text() == schema_markdown(), (
        "regenerate with: uv run python pipeline/alert_text.py --write-schema-doc"
    )
