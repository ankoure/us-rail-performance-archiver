# Static GTFS normalization

> Status: implemented (`pipeline/gtfs.py`). Fills a pre-existing doc gap — the
> static-GTFS fetch/query subsystem (`analysis/static_gtfs.py`,
> `analysis/gtfs_fetcher.py`) had no design doc despite being mature and in
> daily use by `pipeline/gold.py`'s OTP and routes-manifest paths.

## 1. What existed before this

`analysis/gtfs_fetcher.GtfsResolver` resolves a `(feed_id, service_date)` pair
to the correct GTFS static snapshot via the MobilityDatabase archived-feeds
catalog, downloads/caches the zip under `data/static_gtfs/`, and loads it into
an `analysis.static_gtfs.StaticGtfs` — a lazy, in-memory zip reader.
`pipeline/gold.py` uses this to join realtime vehicle visits to their
scheduled trip (`analysis/adherence.py`, the OTP marts) and to build a small
**routes manifest mart** (`ROUTES_SCHEMA`/`_build_routes`).

The routes mart is the one existing precedent for persisting static GTFS as a
parquet mart, and it's deliberately partitioned the same way as every daily
metric — `metrics/routes/feed=/year=/month=/day=/data.parquet` — accepting
full daily duplication of a small, rarely-changing table, purely so
`dashboard/api` can read it through the same S3 hot-bucket day-partition path
as everything else.

Nothing persisted **stops**, **service-calendar**, or **route-shape** data —
`shapes.txt` wasn't read anywhere in the codebase — and every `gold.py` run
re-fetched the catalog and re-parsed the snapshot zip from scratch (in
memory, per process).

## 2. Why day-partitioning doesn't fit here

Static GTFS changes on the order of weeks to months, not daily. Stops,
calendar, and shapes are also meaningfully larger than the routes mart.
Following the routes precedent — duplicate the full table into every day's
partition — would multiply storage for no benefit, and this project has
already been burned by unbounded landing-zone duplication once (913 GiB
before `prune_s3.py` was added). Partition by snapshot instead.

## 3. Design: version-partitioned marts + a day-partitioned pointer

`pipeline/gtfs.py` writes two kinds of mart:

**Version-partitioned** (written once per `(feed, version_slug)`, never
duplicated across the days that version is in effect):

```
metrics/gtfs_stops/feed={feed}/version={version_slug}/data.parquet
metrics/gtfs_calendar/feed={feed}/version={version_slug}/data.parquet
metrics/gtfs_calendar_dates/feed={feed}/version={version_slug}/data.parquet
metrics/gtfs_shapes/feed={feed}/version={version_slug}/data.parquet
```

`version_slug` is `analysis.gtfs_fetcher.Snapshot.version_slug` — the same
key `GtfsResolver` already uses to cache the downloaded zip.

**Day-partitioned** (one tiny row per feed per day, the normal mart shape):

```
metrics/gtfs_versions/feed={feed}/year=/month=/day=/data.parquet
```

A caller thinking in terms of "the schedule in effect on day D" (the way
every other mart is addressed) reads `gtfs_versions` for that day to get
`version_slug`, then reads the version-partitioned marts directly — one extra
small lookup instead of duplicated data.

### MBTA GTFS extensions

Three more version-partitioned marts normalize MBTA's GTFS extension files,
which most non-MBTA feeds simply don't publish — they degrade to an
empty-but-present mart exactly like `gtfs_stops`/`gtfs_calendar` do when the
source file is absent:

```
metrics/gtfs_route_patterns/feed={feed}/version={version_slug}/data.parquet
metrics/gtfs_directions/feed={feed}/version={version_slug}/data.parquet
metrics/gtfs_checkpoints/feed={feed}/version={version_slug}/data.parquet
```

- `gtfs_route_patterns` — `route_patterns.txt`: groups a route+direction's
  trips into named, typicality-ranked patterns (typical / deviation /
  atypical / diversion), each with a `representative_trip_id`.
- `gtfs_directions` — `directions.txt`: human-readable direction labels and
  destinations per `(route_id, direction_id)`, e.g. "Outbound" / "Alewife".
- `gtfs_checkpoints` — `checkpoints.txt`: **reference table only**
  (`checkpoint_id` -> `checkpoint_name`). MBTA's `checkpoint_id` actually
  lives on `stop_times.txt` (per stop_time, i.e. per trip), not on
  `stops.txt` — it isn't guaranteed stable per stop, so this mart
  deliberately does not derive a `stop_id -> checkpoint_id` mapping.
  `StaticGtfs.stop_times` exposes `checkpoint_id` directly (widened to
  include it when present) for a caller who wants that join themselves.

### Calendar is a typed passthrough, not an expansion

`gtfs_calendar`/`gtfs_calendar_dates` mirror `calendar.txt`/`calendar_dates.txt`
with better types (bool weekday flags, ISO date strings), not a resolved
service_id × date table. `StaticGtfs.active_service_ids(date)` is cheap,
in-memory logic — a reader can run the same weekday+exception join themselves
over these two small tables. Precomputing the expansion into the mart would
reintroduce day-linear row growth *inside a version-partitioned mart*
(hundreds of `service_id`s × a multi-month window = tens of thousands of rows
per version) — exactly the growth this design exists to avoid.

### Idempotency

For each `(feed, day)`:

1. Resolve `snap = pick_snapshot(catalog, day)`.
2. If `(feed, snap.version_slug)` hasn't been handled yet this run, and
   either `--force` is set or the four version-mart files aren't all already
   on disk: download/parse the snapshot (`GtfsResolver` already memoizes this
   per snapshot) and write all four marts — **always**, even when a table is
   legitimately empty (e.g. a feed with no `calendar_dates.txt`). Writing an
   empty-but-schema-valid file is what marks that mart "handled"; skipping
   the write on empty rows (as `_build_routes` does for its per-day mart)
   would make an empty table look unhandled forever and re-trigger a full
   fetch+parse every single day.
3. Independently, write today's `gtfs_versions` row if it isn't already
   there — this happens every day regardless of whether the version marts
   needed rebuilding, since a new day under an existing version still needs
   its own pointer row.

Net effect: network fetch happens once per snapshot (via `GtfsResolver`'s
zip cache), CSV parse + parquet write happens once per snapshot per run, and
only the cheap manifest row is genuinely per-day work.

## 4. Batch-chain placement

Runs daily, between `rollup.py` and `gold.py`:

```
pipeline/rollup.py --day $DAY && pipeline/gtfs.py --day $DAY && pipeline/gold.py --day $DAY && pipeline/ship.py --day $DAY && pipeline/cert_check.py
```

It has to run daily regardless of the version-partitioning, because the
`gtfs_versions` manifest needs a fresh row every day by design — a
lower-frequency cron would just accumulate a backfill gap that then needs an
`--all-days` catch-up anyway. Thanks to the idempotency design, most days
this is cheap: a handful of `.exists()` checks plus one small manifest-row
write per feed. Per-feed/per-day failures (missing `mdb_feed_id`,
`LookupError` for an uncovered date, catalog fetch errors) never propagate a
nonzero exit, matching `gold.py`'s self-skip contract, so one bad feed can't
break the `&&` chain.

## 5. Known follow-up (not addressed yet)

`gold.py`'s own GTFS resolver (`_make_gtfs_resolver`, used by OTP and the
routes mart) independently re-parses the same snapshot zip that
`pipeline/gtfs.py` just processed for the same `(agency, day)`. The on-disk
zip cache means the *download* isn't duplicated, but the CSV-parse cost is
paid twice, since `StaticGtfs` instances aren't shared across processes. A
future iteration could have `gold.py` read `gtfs_stops`/`gtfs_versions`
directly instead of resolving GTFS itself — not done here, to keep this
change scoped to adding the new marts.

Also out of scope: `metrics/routes` could migrate to the same
version-partitioned pattern to save its own daily duplication. Worth
revisiting once this pattern has run in prod for a while.
