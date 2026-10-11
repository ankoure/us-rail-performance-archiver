# Alert translation tables

<!-- Generated from analysis/alert_text.py by
     `uv run python pipeline/alert_text.py --write-schema-doc`.
     Don't edit by hand: tests/test_analysis_alert_text.py fails on drift. -->

Schema version **1**, stored as the `schema_version` column and in each file's parquet key-value metadata. Column descriptions are also stored in each file, as the fields' `description` metadata.

Design: [docs/design/alert-translations.md](../design/alert-translations.md). Reader views (`poll_log`, `gaps`): [docs/design/alert-translations.sql](../design/alert-translations.sql).

```
s3://rail-performance-archiver-hot/alert_text/feed={feed_id}/year=YYYY/month=M/day=D/data.parquet
s3://rail-performance-archiver-hot/alert_versions/feed={feed_id}/year=YYYY/month=M/day=D/data.parquet
s3://rail-performance-archiver-hot/alert_payloads/feed={feed_id}/year=YYYY/month=M/day=D/data.parquet
```

Days are UTC days. Every timestamp is `timestamp[us, UTC]`.

A **run** is a maximal stretch of consecutive successfully parsed polls in which a key had one value. A parsed poll with a different value, or without the key, ends it, so A→B→A is three runs and an alert that disappears and returns starts a new one. Failed polls and downtime don't end a run; they set `spans_gap`.

**Not recoverable:** anything before 2026-06-08; any day with no cold tarball (those days have no rows, and aren't reconstructed from the flattened `alerts` parquet or the daily snapshots); lifecycle timestamps from MBTA's enhanced JSON feed, which isn't archived.

## `alert_text`

One row per run of one exact text for one (alert, TranslatedString field, language), clipped to the UTC day.

| column | type | nullable | description |
|---|---|---|---|
| `schema_version` | `int16` | no | Table schema version (also in the file's key-value metadata). |
| `feed_id` | `string` | no | Our feed name, e.g. massachusetts-bay-transportation-authority-mbta-alerts-1603. |
| `mdb_feed_id` | `string` | yes | Mobility Database feed id from config, e.g. mdb-1603. |
| `alert_id` | `string` | no | GTFS-RT FeedEntity.id. |
| `field` | `string` | no | Proto field name of the TranslatedString: header_text, description_text, url, tts_header_text, tts_description_text, image_alternative_text, cause_detail, effect_detail. |
| `language` | `string` | yes | Language tag exactly as published; null when absent or empty. Never defaulted to en. |
| `text` | `string` | no | Text byte-for-byte as published: no trimming, Unicode normalization or whitespace changes. |
| `text_sha256` | `string` | no | Hex SHA-256 of the UTF-8 text, for cheap equality joins. |
| `translation_index` | `int16` | no | Position within translation[] at the run's first poll. Two entries with the same language in one field are separate runs, told apart by this. |
| `run_id` | `string` | no | Stable id of the run, shared by its rows in every day partition. GROUP BY run_id to stitch a run across midnight. |
| `first_seen` | `timestamp[us, tz=UTC]` | no | First poll (fetched_at) in this run, clipped to this day. |
| `last_seen` | `timestamp[us, tz=UTC]` | no | Last poll in this run, clipped to this day. |
| `first_feed_timestamp` | `timestamp[us, tz=UTC]` | yes | FeedHeader.timestamp of the payload at first_seen. |
| `last_feed_timestamp` | `timestamp[us, tz=UTC]` | yes | FeedHeader.timestamp of the payload at last_seen. |
| `poll_count` | `int32` | no | Parsed polls in this row (this day's slice of the run), including 304 and duplicate polls. |
| `continues_from_prev_day` | `bool` | no | The run was already open at the end of the previous day. |
| `continues_next_day` | `bool` | no | The run was still open at this day's last parsed poll. |
| `spans_gap` | `bool` | no | A downtime or failure gap falls inside [first_seen, last_seen], or (for a run continuing from the previous day) between its previous-day last_seen and this first_seen. |

## `alert_versions`

One row per run of one set of non-text alert metadata per alert, clipped to the UTC day. Text changes don't start a version; alert_text covers them.

| column | type | nullable | description |
|---|---|---|---|
| `schema_version` | `int16` | no | Table schema version (also in the file's key-value metadata). |
| `feed_id` | `string` | no | Our feed name, e.g. massachusetts-bay-transportation-authority-mbta-alerts-1603. |
| `mdb_feed_id` | `string` | yes | Mobility Database feed id from config, e.g. mdb-1603. |
| `alert_id` | `string` | no | GTFS-RT FeedEntity.id. |
| `run_id` | `string` | no | Stable id of the run, shared by its rows in every day partition. GROUP BY run_id to stitch a run across midnight. |
| `first_seen` | `timestamp[us, tz=UTC]` | no | First poll (fetched_at) in this run, clipped to this day. |
| `last_seen` | `timestamp[us, tz=UTC]` | no | Last poll in this run, clipped to this day. |
| `first_feed_timestamp` | `timestamp[us, tz=UTC]` | yes | FeedHeader.timestamp of the payload at first_seen. |
| `last_feed_timestamp` | `timestamp[us, tz=UTC]` | yes | FeedHeader.timestamp of the payload at last_seen. |
| `poll_count` | `int32` | no | Parsed polls in this row (this day's slice of the run), including 304 and duplicate polls. |
| `continues_from_prev_day` | `bool` | no | The run was already open at the end of the previous day. |
| `continues_next_day` | `bool` | no | The run was still open at this day's last parsed poll. |
| `spans_gap` | `bool` | no | A downtime or failure gap falls inside [first_seen, last_seen], or (for a run continuing from the previous day) between its previous-day last_seen and this first_seen. |
| `cause` | `string` | yes | Alert.cause enum name; UNKNOWN_ENUM_<n> for a value these bindings don't know; null if unset. |
| `effect` | `string` | yes | Alert.effect enum name; same rules as cause. |
| `severity_level` | `string` | yes | Alert.severity_level enum name; same rules as cause. |
| `cause_detail` | `list<item: struct<text: string, language: string>>` | yes | Alert.cause_detail translations (also emitted as alert_text rows). |
| `effect_detail` | `list<item: struct<text: string, language: string>>` | yes | Alert.effect_detail translations (also emitted as alert_text rows). |
| `active_period` | `list<item: struct<start: timestamp[us, tz=UTC], end: timestamp[us, tz=UTC]>>` | yes | Alert.active_period in published order. Some feeds (MBTA) roll end forward every minute on ongoing alerts, so each roll is a new version. |
| `informed_entity` | `list<item: struct<agency_id: string, route_id: string, route_type: int32, direction_id: int32, trip: struct<trip_id: string, route_id: string, direction_id: int32, start_time: string, start_date: string, schedule_relationship: string>, stop_id: string>>` | yes | Alert.informed_entity in the order published at the run's first poll. Order isn't part of the version: the same selectors in a different order are the same version. |
| `image` | `list<item: struct<url: string, media_type: string, language: string>>` | yes | Alert.image (TranslatedImage.localized_image). |
| `extensions_raw` | `binary` | yes | Every field on the Alert these bindings don't know (unknown fields and extensions), serialized byte-for-byte; null if none. |
| `version_sha256` | `string` | no | SHA-256 over the version columns above, with informed_entity sorted; a change starts a new version run. |

## `alert_payloads`

One row per distinct payload digest the day's polls referenced. Join to metadata on (feed, day, digest) for the per-poll log.

| column | type | nullable | description |
|---|---|---|---|
| `schema_version` | `int16` | no | Table schema version (also in the file's key-value metadata). |
| `feed_id` | `string` | no | Our feed name, e.g. massachusetts-bay-transportation-authority-mbta-alerts-1603. |
| `mdb_feed_id` | `string` | yes | Mobility Database feed id from config, e.g. mdb-1603. |
| `digest` | `string` | no | SHA-256 of the payload; joins to metadata.digest (see the poll_log view). |
| `first_fetched_at` | `timestamp[us, tz=UTC]` | no | First poll on this day that served this payload. |
| `last_fetched_at` | `timestamp[us, tz=UTC]` | no | Last poll on this day that served this payload. |
| `poll_count` | `int32` | no | Polls on this day that served this payload, including 304s and duplicates. |
| `parsed` | `bool` | no | False if the payload failed to parse; the row is kept so failures are visible. |
| `carried_from_prev_day` | `bool` | no | The payload's bytes are only in the previous day's landing (a 304/duplicate of its last payload); these facts were carried from that day's row. |
| `feed_timestamp` | `timestamp[us, tz=UTC]` | yes | FeedHeader.timestamp. |
| `gtfs_realtime_version` | `string` | yes | FeedHeader.gtfs_realtime_version. |
| `incrementality` | `string` | yes | FeedHeader.incrementality enum name. |
| `entity_count` | `int32` | yes | FeedEntity count in the payload. |
| `alert_count` | `int32` | yes | FeedEntities carrying an alert (duplicate ids counted once). |
