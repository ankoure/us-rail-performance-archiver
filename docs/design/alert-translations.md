# Versioned alert translations (for `lost-in-transit`)

Status: **approved 2026-09-28; tables built for the MBTA, not yet deployed.**
The `metadata` schema fix (tutored, below) comes before the backfill.
Column-level reference: [docs/schemas/alert_text.md](../schemas/alert_text.md),
which is generated from the code. Reader views: [alert-translations.sql](alert-translations.sql).

## What changed from the draft (2026-09-28)

- **The fold walks the poll log, not the payloads.** Landing is
  content-addressed: a day's raw bins hold each *distinct* payload once, and
  `digest_timestamps` keeps a digest's *earliest* `fetched_at`. Iterating
  payloads the way `alert_snapshot` does can't see A→B→A, because the second A
  is the same stored bin. The builder decodes each distinct payload once, keyed
  by `sha256(payload)`, which is exactly the poller's `metadata.digest`. It then
  folds runs over the metadata rows, one per poll including 304s and
  duplicates. This needs no `archiver/` change. The snapshot's `poll_count` is
  therefore really a count of distinct payloads.
- **State carries across midnight, not only `run_id`s.** The poller's
  ETag/digest state survives midnight, so a day's first 304 and duplicate polls
  point at a payload stored on the *previous* day. The builder rebuilds that
  payload's content from the previous day's open runs, which are exactly its
  last parsed poll's content, together with its last `alert_payloads` row. It
  marks the row `carried_from_prev_day`. This also covers feeds that don't
  change for days.
- **All timestamps are `timestamp[us, UTC]`.** Parquet has no seconds unit, so
  `timestamp[s]` would come back as `[ms]` anyway.
- **`alert_payloads` gained** `last_fetched_at`, `poll_count` and
  `carried_from_prev_day`. `cause_detail`/`effect_detail` in `alert_versions`
  are `list<struct<text, language>>`.
- **The bindings are older than the spec.** `gtfs-realtime-bindings` 2.0.0 has
  no `image`, `image_alternative_text`, `cause_detail` or `effect_detail`, so
  those arrive as unknown fields. The builder decodes them by field number
  (15–18), and the descriptor walk takes over after a bindings upgrade.
- **The schema reference lives in the repo** (`docs/schemas/alert_text.md`,
  generated, with a drift test), not as `SCHEMA.md` in hot, because shipping
  non-parquet files to hot would need a shipper change. Each column's
  description is also stored in the parquet field metadata, so files describe
  themselves.
- **`alert_languages` waits** until the build widens past the MBTA.
- **The nightly runs inside `pipeline/snapshot.py`** for `ALERT_TEXT_FEEDS`,
  best-effort, right after the day's snapshot. `pipeline/alert_text.py` is for
  backfills and one-off runs.

**Checked against real data (MBTA alerts, 2026-09-27, from S3 landing):**
- The build produced 3,122 text runs, 7,450 version runs and 1,441 payloads, in 8 s at 270 MB peak RSS.
- All 2,884 metadata rows (1,441 `ProtobufResponse`, 1,443 `NotModifiedResponse`) join to an `alert_payloads` row.
- The last run per key matches the day's `snapshots/alerts` text exactly for all 2,428 translations the snapshot has.

Two things the data showed:
- **MBTA does publish `cause_detail`, `effect_detail` and `image_alternative_text`**: 220, 224 and 2 runs that day. The Jul 31 check below couldn't see them, because `MessageToDict` drops unknown fields.
- **MBTA rolls `active_period.end` forward every minute on ongoing delay alerts.** That's why there are about 7k version runs a day for 216 alerts; one alert had 580. They are real versions. Readers who only want substantive changes should compare versions without `active_period`.

## Summary

`lost-in-transit` compares each GTFS-RT service alert's English text with its
translations: accuracy, coverage, and how quickly translations follow English
edits. It starts with the MBTA (`massachusetts-bay-transportation-authority-mbta-alerts-1603`,
`mdb-1603`), which publishes every alert in 8 languages (en, es-419, pt-BR,
fr-FR, ht-HT, zh-CN, zh-TW, vi-VN).

None of our current outputs can answer "what did each language say, and when":

| Output | Languages | History | Problem |
|---|---|---|---|
| `curated/alerts/…/data.parquet` | one | per poll | decoder keeps `en` (else `translation[0]`) as a plain string |
| `snapshots/alerts/…/data.json.gz` | all | last version per day | intra-day edits lost (984 alert-days over 54 days) |
| cold tarballs (`rail-performance-archiver-cold`) | all | every poll | Glacier Deep Archive, up to 48 h restore |

This doc adds three hot, partitioned, versioned parquet tables built in the
nightly batch from the raw polls: **`alert_text`** (one row per run of an exact
text), **`alert_versions`** (non-text metadata runs), and **`alert_payloads`**
(one row per distinct payload, with its header timestamp and entity counts).
The **poll log** is the existing `metadata` table joined to `alert_payloads`,
not a new copy of it. It also adds a small **`alert_languages`** summary, and a
backfill of the MBTA from Jun 8, 2026 using the cold tarballs.

## Key design decisions

1. **Build from the existing snapshot loop, not the poller.**
   `analysis/alert_snapshot.build_alert_snapshot` already decodes every poll's
   alert entities in `fetched_at` order with all translations intact, then
   collapses them last-write-wins. The new builder uses the same input path
   (`iter_payloads` + `digest_timestamps`) and a different fold. No `archiver/`
   changes are needed for the core tables.
2. **A row is a *run*, not a distinct value.** A run is a maximal stretch of
   consecutive polls in which one `(alert, field, language)` had one exact
   text. If the text flips A→B→A, that gives three rows. "Distinct text" alone
   would merge the two A windows and hide the flip-back, which is exactly the
   kind of edit-then-revert `lost-in-transit` needs to see.
3. **Only changes are written.** Each unchanged poll just extends the current
   run (`last_seen`, `poll_count`), so the table grows with edits, not with
   polls.
4. **Partitioned by UTC day, with a stable `run_id` across days.** A run that
   spans midnight gets one row in each day partition, each clipped to that day's
   polls. All of the rows share the same `run_id`. Readers `GROUP BY run_id` for
   the full window (see [Querying](#querying)).
5. **The poll log is the metadata table we already have.** Every poll attempt
   already writes a metadata row (200, 304, content-hash duplicate, HTTP error,
   transport error), and the rollup already ships it to hot as
   `metadata/feed=…/data.parquet`. We don't duplicate it. We add only what it
   lacks, the decoded per-payload facts, keyed by digest (`alert_payloads`), and
   separately fix its schema drift (see [Poll log](#poll-log-metadata--alert_payloads)).

## Tables

All paths are under the curated tree. That tree is mirrored to
`s3://rail-performance-archiver-hot` (S3 Standard) by the existing hot-ship
step, so no restore is ever needed to read them.

```
{curated}/alert_text/feed={feed_id}/year=YYYY/month=M/day=D/data.parquet
{curated}/alert_versions/feed={feed_id}/year=YYYY/month=M/day=D/data.parquet
{curated}/alert_payloads/feed={feed_id}/year=YYYY/month=M/day=D/data.parquet
{curated}/alert_languages/data.parquet            # rebuilt nightly, all feeds
{curated}/metadata/feed={feed_id}/…               # existing, the poll log (schema fixed, see below)
```

This uses the same `year=/month=/day=` (unpadded) convention as every other
curated table, so the existing `curated_fs` / DuckDB hive reads work unchanged.
`feed_id` is our feed name. The MDB id (`mdb_feed_id` in `feeds.yaml`) is
stored as a column so `lost-in-transit` can join on either.

Every file carries `schema_version` in its parquet key-value metadata *and* as
a column (see [Schema versioning](#schema-versioning)).

### `alert_text` (the translations table)

| column | type | notes |
|---|---|---|
| `schema_version` | int16 | `1` |
| `feed_id` | string | e.g. `massachusetts-bay-transportation-authority-mbta-alerts-1603` |
| `mdb_feed_id` | string, nullable | e.g. `mdb-1603` |
| `alert_id` | string | GTFS-RT `FeedEntity.id` |
| `field` | string | proto field name of the `TranslatedString`, see below |
| `language` | string, nullable | exactly as published. Absent **or** `""` becomes null; never defaulted to `en` |
| `text` | string | byte-for-byte as published, with no trim, no Unicode normalization, no whitespace collapse |
| `text_sha256` | string | hex digest of the UTF-8 `text`, for cheap equality joins |
| `translation_index` | int16 | position within `translation[]` in the first poll of the run. Lets readers reconstruct what `translation[0]` was |
| `run_id` | string | stable across day partitions (see below) |
| `first_seen` | timestamp[us, UTC] | first poll (`fetched_at`) in this run, clipped to this day |
| `last_seen` | timestamp[us, UTC] | last poll in this run, clipped to this day |
| `first_feed_timestamp` | timestamp[s, UTC], nullable | `FeedHeader.timestamp` at `first_seen` |
| `last_feed_timestamp` | timestamp[s, UTC], nullable | `FeedHeader.timestamp` at `last_seen` |
| `poll_count` | int32 | polls in this row (this day's slice of the run) |
| `continues_from_prev_day` | bool | the run was already open at this day's first poll |
| `continues_next_day` | bool | the run was still open at this day's last poll |
| `spans_gap` | bool | a [gap](#gaps) falls strictly inside `[first_seen, last_seen]` |

**Which fields.** Fields are discovered, not listed. The builder walks each
`Alert` message's descriptor and emits a row for **every set field whose type is
`TranslatedString`**. In the current bindings those are `header_text`,
`description_text`, `url`, `tts_header_text`, `tts_description_text` and
`image_alternative_text`. A future binding upgrade picks up new ones
automatically. `image` (`TranslatedImage`) has a different shape
(`url`/`media_type`/`language`) and goes in `alert_versions.image` instead.

**Run semantics, precisely:**

- A poll *observes* an `(alert, field, language)` if that alert entity is
  present and that translation exists in it.
- A run ends at the first poll that is **successfully parsed** and either
  shows a different text or doesn't observe the key. That covers an alert that
  disappears, a field that's dropped, and a language that's dropped.
- Polls that fail to parse, transport errors and HTTP errors **do not end a
  run**, because they are not evidence either way. Downtime doesn't end a run
  either. Both show up through `spans_gap` and the poll log instead of
  silently splitting or merging runs.
- An alert that disappears and comes back with the same text gets a new run,
  because there was a parsed poll in between that didn't observe it.
- A duplicate language in one alert (two `translation` entries with the same
  `language`) is kept as two keys, distinguished by `translation_index`. We
  don't expect this, but we won't silently pick one.

**`run_id`.** `sha256(feed_id, alert_id, field, language, text, run_start)`
truncated to 16 bytes hex, where `run_start` is the run's true first
`fetched_at`, which may be on an earlier day. To keep it stable across days, the
builder for day *D* reads day *D−1*'s `alert_text` rows with
`continues_next_day = true` and carries their `run_id`s forward if the text
still matches at *D*'s first parsed poll. If *D−1* has no partition (a
downtime day), runs start fresh and `continues_from_prev_day` is false. This
makes the build **order-dependent within a feed**: the backfill must run days
in ascending order (see [Backfill](#backfill)). Nightly runs already do.

### `alert_versions` (non-text metadata)

Same run rules, keyed by `(feed_id, alert_id)`. A new run starts whenever any
tracked metadata value changes. Text changes do **not** start a new version
here, because `alert_text` already covers them.

| column | type | notes |
|---|---|---|
| `schema_version`, `feed_id`, `mdb_feed_id`, `alert_id`, `run_id` | | as above |
| `first_seen`, `last_seen`, `first_feed_timestamp`, `last_feed_timestamp`, `poll_count`, `continues_from_prev_day`, `continues_next_day`, `spans_gap` | | as above |
| `cause` | string, nullable | enum **name** (`CONSTRUCTION`); unknown numeric values become `"UNKNOWN_ENUM_<n>"`, never dropped |
| `effect` | string, nullable | same |
| `severity_level` | string, nullable | same |
| `cause_detail`, `effect_detail` | struct(`field`, `translation` list) nullable | these are `TranslatedString`s too, so they are **also** emitted in `alert_text`. Here they're kept only so the version row is self-contained |
| `active_period` | list<struct<start: timestamp[s,UTC], end: timestamp[s,UTC]>> | order preserved |
| `informed_entity` | list<struct<agency_id, route_id, route_type: int32, direction_id: int32, trip: struct<trip_id, route_id, direction_id, start_time, start_date, schedule_relationship: string>, stop_id>> | order preserved |
| `image` | list<struct<url, media_type, language>>, nullable | `TranslatedImage.localized_image` |
| `extensions_raw` | binary, nullable | serialized unknown fields / extensions on the `Alert` message (see below) |
| `version_sha256` | string | digest of all the above, used for change detection |

**Agency extensions: a finding that changes the plan.** The spec asks for
created / last-modified / lifecycle timestamps. **MBTA's `Alerts.pb` doesn't
carry them.** A Jul 31 snapshot has exactly `informed_entity, cause, effect,
header_text, severity_level, active_period, description_text, url` on its 314
alerts, and no extension fields. (That list came from `MessageToDict`, which hides fields the bindings don't know; the real payloads also carry `cause_detail`, `effect_detail` and `image_alternative_text`. None of those are lifecycle timestamps, so the finding stands.) MBTA publishes `created_timestamp`,
`last_modified_timestamp`, `alert_lifecycle`, `duration_certainty` etc. only in
its **enhanced JSON feed** (`/realtime/Alerts_enhanced.json`), which we don't
poll.

The plan has two parts:

- **In this design:** `extensions_raw` preserves any unknown or extension
  fields on the proto `Alert` byte-for-byte (via
  `google.protobuf.unknown_fields.UnknownFieldSet`; the old
  `Message.UnknownFields()` is gone in upb protobuf), so nothing another agency
  sends is lost. Typed
  decoding of known extensions can be added later without a backfill,
  because the bytes are there.
- **Recommended follow-up, not in scope here:** add an
  `massachusetts-bay-transportation-authority-mbta-alerts-enhanced` feed polling
  `Alerts_enhanced.json`. That is a `feeds.yaml` + `archiver/` change (JSON
  feed, `decoder: raw`, same pattern as the LIRR onboarding), then a
  `alert_versions_ext` mart keyed on the same `alert_id`. It is forward-only:
  the enhanced JSON was never archived, so those timestamps can't be
  recovered before its first poll. Until then, `lost-in-transit` has to infer
  "English edited" from the `en` rows' `first_seen`, which is exact to the
  poll interval (30 s).

### Poll log (`metadata` ⋈ `alert_payloads`)

**What already exists.** The rollup ([archiver/rollup.py](../../archiver/rollup.py)
`_rollup_metadata`) turns each day's metadata jsonl into
`metadata/feed=…/data.parquet` and ships it to hot. It is already one row per
poll attempt, including polls that stored nothing. MBTA alerts on 2026-09-20:
2,881 rows (1,439 `ProtobufResponse`, 1,442 `NotModifiedResponse`). It is
missing three things `lost-in-transit` needs:

1. **Feed timestamp.** It has never been recorded.
2. **Entity counts since Aug 21, 2026.** `vehicle_count` / `trip_update_count` /
   `alert_count` were dropped with the per-poll entity walk (the out-of-memory
   fix). They are present on 2026-08-10 and absent on 2026-09-20.
3. **A stable schema.** The table is written with `paj.read_json`, so the
   schema is *inferred per day*:
   - The count columns come and go.
   - Error columns (`error_type`, `error_message`, `error_body`) only exist on
     days with errors.
   - `timestamp` is a float of epoch seconds.

   This is the drift requirement 4 warns about.

**`alert_payloads`: new, one row per distinct digest per day.** The alert
builder already decodes every distinct payload, so it writes these facts down
as a by-product:

| column | type | notes |
|---|---|---|
| `schema_version`, `feed_id`, `mdb_feed_id` | | |
| `digest` | string | join key to `metadata.digest` |
| `first_fetched_at` | timestamp[us, UTC] | from `digest_timestamps` |
| `parsed` | bool | false = the payload failed to parse (row kept so failures are visible) |
| `feed_timestamp` | timestamp[s, UTC], nullable | `FeedHeader.timestamp` |
| `gtfs_realtime_version`, `incrementality` | string, nullable | header |
| `entity_count` | int32, nullable | |
| `alert_count` | int32, nullable | entities with `HasField("alert")` |

The poll log is then
`metadata LEFT JOIN alert_payloads USING (feed_id, digest)`.
- Duplicate and 304 rows get the right values, because their `digest` is the
  digest of the content they repeat. For 304s the poller pre-seeds the prior
  digest.
- Transport errors have no digest, so their decoded columns are null, which
  is correct.
- The counts are computed in the batch, not the poller, so the removed
  entity walk isn't brought back.

`SCHEMA.md` ships a DuckDB `poll_log` view with this join, so readers don't
have to write it.

**Fixing `metadata`'s schema drift (separate change, `archiver/` → tutored).**
`_rollup_metadata` should write with an explicit `pa.schema`:
- every column the poller can emit, missing ones as typed nulls;
- `timestamp` as `timestamp[us, UTC]`;
- `schema_version` in the key-value metadata.

**Done (2026-10-07):** [archiver/metadata_schema.py](../../archiver/metadata_schema.py)
declares 13 columns: the 11 above the poller has written, plus
`DecodeFailureResponse`'s `drift_missing_required` / `drift_extras`. A
subclass-walk test keeps it in step with `response.py`. Unusable lines (torn
appends) are skipped and counted in the `skipped_lines` key-value field, up
to 3 per feed-day. A row with a key the schema doesn't have fails the
feed-day: no marker is written, landing is kept, and the existing "Batch step
errored" monitor alerts on the failed `rollup.day` span. `alert_text` reads
polls through the same parser and `to_us`, so `fetched_at` equals
`metadata.timestamp` for the same poll.

Old partitions are rewritten in place by
[pipeline/rewrite_metadata_schema.py](../../pipeline/rewrite_metadata_schema.py)
(hot copies only; the jsonl in cold is untouched). It deduplicates on the same
key as `read_metadata`, so it also removes the 9/7–9/8 duplicates. The
2026-10-07 footer inventory: 44,741 partitions (8.9 GB), all inferred-era. Every
column seen is in the schema with the declared type: one partition has the
`drift_*` columns, and 1,226 have no `digest` column. A dry run of the three
MBTA feeds converted 351/351 partitions and removed 9,064 duplicate rows, all
on 9/7 and 9/8. The `poll_log` view now reads `timestamp` directly, so it needs
every partition rewritten.

### Gaps

A gap is a period with no successful poll. It is *derived* from the poll log,
not stored separately, so it can never disagree with it:

- **Downtime gap:** consecutive poll-log rows more than
  `3 × poll_interval_seconds` apart. That's 90 s for the MBTA.
- **Failure gap:** a stretch of consecutive rows with no successful outcome
  (every row is an error, transport error, or a digest with `parsed = false`).
- **Missing day:** a UTC day with no `metadata` partition at all.

The builder writes a view definition (`docs/design/alert-translations.sql`,
DuckDB) that computes `gaps(feed_id, gap_start, gap_end, kind)`. That view is
also what fills in `spans_gap`.

**Known gaps to verify during backfill:**

| Date(s) | Reported missing from | Expectation |
|---|---|---|
| Aug 23–24 | every output | probably real downtime, which will appear as missing days / downtime gaps |
| Aug 13, 14, 16 | rollups only | the raw data may exist in cold, and if so the backfill recovers it |
| Aug 26–31 | (landing archive gap, Sep 4 backfill) | 32/48 agency-days recovered; confirm whether MBTA alerts were among them |

The backfill run log records, per day, whether a cold tarball existed and how
many polls it held. That table goes into this doc's appendix once it runs.

### `alert_languages` (the per-feed summary; deferred until the build widens past the MBTA)

Rebuilt nightly from `alert_text` over a trailing 30 days, for every
alerts-capable feed. It is small enough to keep as one unpartitioned file.

| column | notes |
|---|---|
| `feed_id`, `mdb_feed_id` | |
| `language` | nullable (null = untagged) |
| `field` | |
| `alerts_with_language` | distinct `alert_id`s having this `(field, language)` |
| `alerts_total` | distinct `alert_id`s in the window |
| `coverage` | ratio of the two |
| `first_seen`, `last_seen` | |

The point is to answer "which other feeds are worth comparing with the MBTA":
filter to feeds with more than one non-null language. Because `alert_text`
has the same schema for every feed, this works across all ~150 alerts feeds
with no per-agency code.

## Honest formats and schema versioning

- **Parquet only**, written with pyarrow (zstd, dictionary encoding on
  `feed_id`/`alert_id`/`field`/`language`). We add no new `.json.gz` outputs,
  and the existing snapshot `.json.gz` files are already real gzip
  (`gzip.open`).
  The zlib-not-gzip problem the spec mentions belongs to the separate
  mobility-database archiver, not this repo.
- **Explicit schemas.** Each table's `pa.schema` lives in one module
  (`analysis/alert_text.py`), and files are written with that schema, never
  inferred. This avoids the naming drift we had in `trip_updates`/`vehicles`
  (see [rt-schema-drift.md](rt-schema-drift.md)).
- **`schema_version`** is stored as a column and in the file's key-value
  metadata. The rules:
  - Adding a nullable column is a minor change and keeps the version.
  - Renaming, retyping, removing a column or changing run semantics bumps
    the version.
  - Older partitions are then rewritten by the same backfill tool, so a
    whole-table read never mixes versions.
- **Schema doc for readers:** [docs/schemas/alert_text.md](../schemas/alert_text.md),
  generated from the `pa.schema` objects
  (`uv run python pipeline/alert_text.py --write-schema-doc`; a test fails on
  drift). The same column descriptions are stored in every file's field
  metadata, so `lost-in-transit` can read them without this repo.

## Pipeline placement

- **New module `analysis/alert_text.py`:** the pure builder
  `build_alert_tables(feed, day, source, prev_day_open_runs) -> (text, versions, payloads)`.
  It shares `iter_payloads` / `digest_timestamps` with `alert_snapshot`.
- **New entry point `pipeline/alert_text.py`:** CLI mirroring
  `pipeline/snapshot.py` (`--feed`, `--day`, `--all-days`, `--curated-dir`,
  `--landing-dir` for extracted cold tarballs). It reads the previous day from
  local curated, then from the hot bucket.
- **Batch:** runs in the existing snapshot stage, right after
  `build_alert_snapshot`, for the feeds in `ALERT_TEXT_FEEDS` (MBTA only for
  now). The new parquets match the hot ship's `*/feed=…/data.parquet` glob, so
  shipping needs no change. Prune doesn't wait on these tables: if a nightly
  fails, the day can be rebuilt from its cold tarball. That stage already reads landing
  for alerts feeds before the prune step, so the new tables add no extra
  landing reads. It runs best-effort per feed, like OTP, so one bad feed
  doesn't block the stage.
- **Memory:** it has the same shape as the snapshot fix from Sep 6. Each poll
  is reduced to its alert entities immediately, so combined feeds (BKK, VBB, …)
  don't hold a day of trip updates. The run state is O(active alerts × fields ×
  languages), a few thousand entries for the MBTA.
- **Keep or retire the daily snapshot?** Keep it for now, because
  `dashboard/api`'s alerts endpoint reads it. The new tables can replace it
  later.

## Backfill

**Scope:** the MBTA alerts feed, 2026-06-08 through the first nightly run
of the new builder.

1. **Inventory:** list the cold keys
   `…/massachusetts-bay-transportation-authority-mbta-alerts-1603/year=2026/month=*/day=*.tar.gz`,
   record sizes, and flag missing days *before* restoring.
2. **Restore:** `RestoreObject` with the **Bulk** tier (up to 48 h,
   ~$0.0025/GB), 7-day expiry. At MBTA-alerts volumes (single-digit MB/day
   after dedup) this should cost well under $1. The inventory in step 1 gives
   the real number, which gets recorded here before we restore.
3. **Build:** a `Source` implementation over restored tarballs (the tar layout
   is the landing layout, so `iter_bins` / `read_metadata` map straight onto
   it). Days are processed **in ascending order** so `run_id`s carry forward.
   Output is written to local curated, then shipped to hot through the normal
   path.
4. **Verify:**
   - Every non-error `metadata` digest for the day has a matching
     `alert_payloads` row, so the poll-log join has no unexplained nulls.
     `metadata` itself already exists in hot for these days and isn't rebuilt.
   - For a sample of days, the last run per key in `alert_text` matches the
     existing `snapshots/alerts` data.json.gz text exactly. That's a free
     cross-check against an independent code path.
   - The "984 intra-day English edits across 54 days" figure is reproduced
     from `alert_text`.

**What can't be recovered (this goes in `SCHEMA.md` too):**

- Anything before 2026-06-08.
- Any day with no cold tarball. Those days will have no rows, and we **do not**
  backfill them from the flattened `curated/alerts` parquet or the
  last-of-day snapshots, because that would make up windows and languages we
  never observed. They appear as missing days in `gaps`.
- The MBTA enhanced-JSON timestamps, at any date before a new feed starts
  polling them.

## Querying

Full timeline for one alert, all languages, across days (DuckDB over hot S3):

```sql
SELECT field, language, text,
       min(first_seen) AS first_seen, max(last_seen) AS last_seen,
       sum(poll_count) AS poll_count, bool_or(spans_gap) AS spans_gap
FROM read_parquet('s3://rail-performance-archiver-hot/alert_text/feed=massachusetts-bay-transportation-authority-mbta-alerts-1603/*/*/*/data.parquet',
                  hive_partitioning = true)
WHERE alert_id = '123456'
GROUP BY run_id, field, language, text
ORDER BY field, language, first_seen;
```

This is followed by a join to `gaps` over the alert's overall window. Together
they are the "done when" query: every version of the English text and each
translation, with its exact first and last poll, and the gaps listed next to
it.

Useful derived measure, **translation lag**: for each `en` run, the delay until
each other language's next run starts, where
`first_seen(lang run) − first_seen(en run)`. Because runs are exact to the
poll interval, lag resolution is 30 s for the MBTA.

## Decisions (2026-09-28)

1. **Run definition:** as drafted. A→B→A is three rows, and errors and downtime don't end a run.
2. **Enhanced MBTA JSON:** later. It's a follow-up, and every day it waits is
   lifecycle data lost for good.
3. **`language` null vs `""`:** fold both to null, as the spec says. A
   `language_raw_present` bool can be added later as a minor (nullable)
   column if anyone needs it.
4. **Scope:** MBTA only in the nightly job until `lost-in-transit` validates the schema.
5. **`metadata` schema fix:** **before** the backfill.
6. **Snapshot retirement:** out of scope. `snapshots/alerts` stays.

### The questions as drafted

1. **Run definition:** are you OK with A→B→A giving three rows and errors not
   breaking a run? The alternative, one row per distinct text with a list of
   windows, is more compact but harder to query.
2. **Enhanced MBTA JSON:** should we start polling `Alerts_enhanced.json` now?
   It's forward-only, so every day we wait is lost lifecycle data. It needs an
   `archiver/` change (tutored).
3. **`language` null vs `""`:** the spec says to fold both to null. That loses
   a small signal (a feed that sets `""` explicitly vs omitting it). Should we
   add a `language_raw_present` bool, or fold as specified?
4. **Scope of the nightly job:** should it run for all ~150 alerts feeds from
   day one (cheap, and it feeds `alert_languages`), or MBTA-only until
   `lost-in-transit` validates the schema?
5. **`metadata` schema fix:** should it be done before the MBTA backfill, so
   `lost-in-transit`'s first reads see a clean table, or after, with readers
   relying on `union_by_name` in the meantime? It's tutored `archiver/` work.
6. **Snapshot retirement:** is it a goal to move `dashboard/api` onto
   `alert_versions` and drop `snapshots/alerts` eventually?

## Appendix: MBTA snapshot check (2026-07-31)

(Superseded on 2026-09-28: this check used the snapshot's `MessageToDict`
output, which can't show fields the bindings don't know. The 2026-09-27 build
found `cause_detail`, `effect_detail` and `image_alternative_text` on MBTA
alerts. See [What changed from the draft](#what-changed-from-the-draft-2026-09-28).)

314 alerts. All 8 languages present on every `header_text` except one alert
missing `zh-CN` (313/314). `description_text` is on 106 alerts and `url` on 11,
both with all 8 languages. There are no extension fields. This single missing
`zh-CN` header is the kind of coverage gap `alert_languages` and
`lost-in-transit` should surface.
