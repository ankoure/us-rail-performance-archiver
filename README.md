# us-rail-performance-archiver

A polling archiver for U.S. transit-agency [GTFS-Realtime](https://gtfs.org/realtime/) feeds. It snapshots raw protobuf/JSON payloads from ~26 agencies on a fixed cadence, rolls them up into queryable Parquet, and (optionally) ships everything to S3 — cold raw bundles to Deep Archive, hot Parquet to standard storage.

**Status:** alpha / single-host. Built primarily as an OOP / data-engineering exercise. Schemas and config layout may still change.

---

## Why it exists

Most agencies publish GTFS-RT as a live snapshot — there's no historical archive you can query. This project closes that gap:

- **Land everything first, transform later.** Every poll is durably written to disk before any decoding happens, so a schema change or a parsing bug can't lose history. Reprocess from the raw payloads any time.
- **Per-agency quirks are a strategy-pattern problem.** MTA's NYCT extensions, MARTA's bespoke JSON API, and the standard GTFS-RT protobuf all flow through the same archiver via pluggable `Parser` + `Decoder` classes registered by name in [feeds.yaml](config/feeds.yaml).
- **Hot path stays simple.** The poller does HTTP + parse + write — no Parquet buffering in memory, no S3 round-trips per poll. Rollup and shipping are separate processes you run on their own cadence.

---

## Two-tier architecture

```
                  ┌──────────────────────┐
                  │   GTFS-RT endpoints  │
                  └──────────┬───────────┘
                             │ HTTP poll (default 60s)
                             ▼
                  ┌──────────────────────┐
                  │      main.py         │  ← FeedArchiver
                  │  (landing-zone hot)  │
                  └──────────┬───────────┘
                             │ writes raw .bin + jsonl metadata
                             ▼
              archive/<feed>/raw/year=…/month=…/day=…/<ts>.bin
              archive/<feed>/metadata/year=…/month=…/day=…/data.jsonl

                             │ (separately, on a cron)
                             ▼
                  ┌──────────────────────┐
                  │     rollup.py        │  ← Rollup
                  │  (curated cold)      │
                  └──────────┬───────────┘
                             │ decodes protobuf → typed rows → parquet
                             ▼
              curated/<kind>/feed=…/year=…/month=…/day=…/data.parquet

                             │ (optional, on a cron)
                             ▼
                  ┌──────────────────────┐
                  │      ship.py         │  ← Shipper
                  └──────────┬───────────┘
                             │
                  ┌──────────┴───────────┐
                  ▼                      ▼
        s3://cold-bucket/         s3://hot-bucket/
        <feed>/year=…/…tar.gz     <kind>/feed=…/…parquet
        (DEEP_ARCHIVE)            (standard)
```

The landing zone is append-only and complete per poll. The curated zone is reproducible from the landing zone, so schema changes are safe — re-run `rollup.py --force`.

---

## Quickstart

### Requirements

- Python 3.13 (managed via [`uv`](https://docs.astral.sh/uv/) — `.python-version` pins it)
- Optional: Docker / Docker Compose, a Datadog agent, an AWS account

### Local dev

```bash
uv sync
cp .env.example .env   # fill in any API keys you need (see "Auth" below)
uv run main.py --frequency 60 --polls 10
```

That polls every feed in `config/feeds.yaml` ten times, 60 seconds apart, and writes to `./archive/`. Drop `--polls` to run forever.

### Docker Compose

```bash
docker compose up -d
```

The compose file mounts `./config` read-only and `./archive` read-write, loads `.env` for API keys, and runs `python main.py --frequency 15`. It doesn't currently invoke `rollup.py` or `ship.py` — run those out-of-band (cron, GitHub Actions, etc.).

---

## CLI reference

Three entrypoints, each loads `config/feeds.yaml` and `.env`.

### `main.py` — archive feeds

```
uv run main.py [-n POLLS] [-f FREQUENCY] [-v]
```

| flag | default | meaning |
|---|---|---|
| `-n / --polls` | infinite | number of polls before exiting |
| `-f / --frequency` | `60` | seconds between polls |
| `-v / --verbose` | off | enable DEBUG logging |

Polls every configured feed sequentially per tick. A transport error (timeout, DNS, etc.) is captured as a `TransportErrorResponse` metadata row — the loop never crashes on a single bad feed.

### `rollup.py` — landing → curated Parquet

```
uv run rollup.py [--feed NAME] [--day YYYY-MM-DD] [-f] [-v]
```

| flag | meaning |
|---|---|
| `--feed` | restrict to one feed name (e.g. `bart-trips`) |
| `--day` | restrict to one UTC day |
| `-f / --force` | re-roll even if the output parquet already exists |
| `-v / --verbose` | DEBUG logging |

Discovers any day-partition in `archive/<feed>/metadata/...` older than *today UTC* and emits one Parquet per output kind (metadata, vehicles, trip_updates, alerts, marta_predictions). Today's partition is intentionally skipped — it's still being written to.

### `ship.py` — curated + landing → S3

```
uv run ship.py [--feed NAME] [--day YYYY-MM-DD] [--force] [-v]
```

Requires `s3.enabled: true` in `feeds.yaml` and AWS creds in the environment. For every day-partition older than today:

- **Cold tier:** tars `raw/ + metadata/` for the day and uploads to `s3://<cold_bucket>/<cold_prefix><feed>/year=…/month=…/day=…tar.gz` with `StorageClass=DEEP_ARCHIVE`.
- **Hot tier:** uploads each curated Parquet under `s3://<hot_bucket>/<hot_prefix><kind>/feed=…/year=…/month=…/day=…/data.parquet`.

Existing keys are skipped unless `--force` is set. (Today's partition is skipped because the archiver is still writing to it.)

---

## Configuration

### `config/feeds.yaml`

Top-level keys (see [archiver/config.py](archiver/config.py) for the full pydantic schema):

```yaml
writer:
  landing_dir: ./archive
  curated_dir: ./curated

telemetry:
  enabled: false              # set true to emit DogStatsD metrics
  service: rail-archiver
  env: dev
  agent_host: localhost
  statsd_port: 8125
  tags: {}

s3:
  enabled: false              # set true to use ship.py
  region: us-east-1
  cold_bucket: my-cold-bucket
  hot_bucket:  my-hot-bucket
  cold_prefix: ""             # e.g. "raw/"
  hot_prefix:  ""             # e.g. "curated/"

agencies:
  - agency_id: BART
    name: Bay Area Rapid Transit
    region: San Francisco Bay Area
    base_url: https://api.bart.gov/gtfsrt
    auth:
      type: none              # one of: none | api_key | bearer | basic
    feeds:
      - name: bart-trips      # globally unique across all agencies
        path: /tripupdate.aspx
        expected_format: protobuf   # protobuf | json | auto
        decoder: standard           # standard | mta_nyct | marta_json
```

Validation enforces unique agency names and globally-unique feed names.

### Auth

Credentials never live in `feeds.yaml` — they're referenced by env-var name and read from the process environment (loaded from `.env` by `python-dotenv`).

| `auth.type` | YAML keys | env vars consumed |
|---|---|---|
| `none` | — | — |
| `api_key` | one of `header:` or `param:`, plus `env:` | the named `env` |
| `bearer` | `env:` | the named `env` |
| `basic` | `username_env:`, `password_env:` | both |

A missing required env var raises at startup, not on first request.

### Supported agencies

26 active agencies are configured today, including BART, MTA NYCT, SEPTA (rail + bus), WMATA, MARTA, Metra, Caltrain, Sound Transit, TriMet, RTD, SFMTA/VTA, MetroLink (STL), CATS, GCRTA, MetroTransit (Twin Cities), PRT (Pittsburgh), SacRT, Valley Metro, UTA, METRO Houston, Tri-Rail, ACE, Capitol Corridor, and SMART. Several more (CTA, MBTA, LACMTA, DART, Miami-Dade, Baltimore MTA, NCTD, Embark, HRT, MTS) are present but commented out — see notes in [feeds.yaml](config/feeds.yaml).

---

## On-disk layout

### Landing zone

```
archive/
└── <feed-name>/
    ├── raw/
    │   └── year=YYYY/month=M/day=D/
    │       └── <unix-timestamp>.bin        # one file per poll
    └── metadata/
        └── year=YYYY/month=M/day=D/
            └── data.jsonl                  # one row per poll (appended)
```

- Partition keys come from the response's capture time (UTC), not wall-clock at write time.
- Raw `.bin` files are written via `*.bin.tmp` → rename, so a crash mid-write can't leave a half-payload.
- A poll with no body (transport error) writes only the metadata row.

### Curated zone

```
curated/
├── metadata/feed=…/year=…/month=…/day=…/data.parquet
├── vehicles/feed=…/year=…/month=…/day=…/data.parquet
├── trip_updates/feed=…/year=…/month=…/day=…/data.parquet
├── alerts/feed=…/year=…/month=…/day=…/data.parquet
└── marta_predictions/feed=…/year=…/month=…/day=…/data.parquet
```

Partitions are hive-style so PyArrow / DuckDB / Spark / Athena will read them as a partitioned dataset directly. `agency` is intentionally a *column* on each row rather than an outer partition, since feed names are already globally unique.

### S3 keys

```
s3://<cold_bucket>/<cold_prefix><feed>/year=Y/month=M/day=D.tar.gz
s3://<hot_bucket>/<hot_prefix><kind>/feed=…/year=…/month=…/day=…/data.parquet
```

---

## Output schemas

### `metadata` (one row per poll)

| column | type | notes |
|---|---|---|
| `timestamp` | double | epoch seconds, UTC, when the poll was received |
| `content_type` | string | HTTP `Content-Type` header |
| `status_code` | int | HTTP status |
| `response_type` | string | `ProtobufResponse` / `JsonResponse` / `ErrorResponse` / `UnknownResponse` / `TransportErrorResponse` |
| `vehicle_count` | int | populated only for `ProtobufResponse` |
| `trip_update_count` | int | populated only for `ProtobufResponse` |
| `alert_count` | int | populated only for `ProtobufResponse` |
| `error_body` | string | first 500 chars of body, only on `ErrorResponse` |
| `error_type` / `error_message` | string | populated only on `TransportErrorResponse` |

### `vehicles` (decoder: `standard`, `mta_nyct`)

`feed_timestamp`, `vehicle_id`, `vehicle_label`, `trip_id`, `route_id`, `direction_id`, `start_date`, `schedule_relationship`, `latitude`, `longitude`, `bearing`, `speed`, `current_stop_sequence`, `stop_id`, `current_status`, `occupancy_status`, `occupancy_percentage`, `vehicle_timestamp`. See [VehicleRow](archiver/decoder.py#L39).

### `trip_updates` (decoder: `standard`, `mta_nyct`)

One row per `StopTimeUpdate` (not per trip). `feed_timestamp`, `trip_id`, `route_id`, `direction_id`, `start_date`, `start_time`, `schedule_relationship`, `vehicle_id`, `vehicle_label`, `stop_sequence`, `stop_id`, `arrival_delay`, `arrival_time`, `arrival_uncertainty`, `departure_delay`, `departure_time`, `departure_uncertainty`, `stop_time_schedule_relationship`. See [StopTimeUpdateRow](archiver/decoder.py#L61).

### `alerts` (decoder: `standard`, `mta_nyct`)

One row per `InformedEntity` (a single alert may produce many rows). `feed_timestamp`, `alert_id`, `cause`, `effect`, `severity_level`, `header_text`, `description_text`, `url`, `agency_id`, `route_id`, `route_type`, `direction_id`, `trip_id`, `stop_id`. See [AlertRow](archiver/decoder.py#L85).

### `marta_predictions` (decoder: `marta_json`)

MARTA exposes its own JSON prediction API (not GTFS-RT). See [MartaPredictionRow](archiver/decoder.py#L21).

---

## Adding a new feed

1. **Find the endpoint.** Check the agency's developer portal for a GTFS-RT URL.
2. **Add the agency to [config/feeds.yaml](config/feeds.yaml):**
   ```yaml
   - agency_id: NEW_AGENCY
     name: My Transit Agency
     region: Somewhere
     base_url: https://example.com/gtfs-rt
     auth:
       type: api_key
       header: X-API-Key
       env: NEW_AGENCY_API_KEY
     feeds:
       - name: newagency-vehicles    # must be globally unique
         path: /VehiclePositions.pb
         expected_format: protobuf
         decoder: standard
   ```
3. **Add the secret to `.env`** (and rotate it if you've ever exposed it).
4. **Pick a decoder.**
   - Standard GTFS-RT protobuf: `decoder: standard`.
   - GTFS-RT with proprietary extensions: subclass `StandardDecoder` in [archiver/decoder.py](archiver/decoder.py) and `@Decoder.register("...")` it.
   - Non-GTFS-RT JSON: subclass `Decoder` directly (see `MartaJsonDecoder` for the pattern).
5. **Run it once with `-n 1 -v`** to confirm the response parses and metadata looks sane before letting it loop.

---

## Module map

| path | role |
|---|---|
| [main.py](main.py), [rollup.py](rollup.py), [ship.py](ship.py) | CLI entrypoints — parse args, build, run |
| [archiver/loader.py](archiver/loader.py) | composition root — wires config → objects |
| [archiver/config.py](archiver/config.py) | pydantic models for `feeds.yaml` |
| [archiver/archiver.py](archiver/archiver.py) | `FeedArchiver` — the poll loop |
| [archiver/auth.py](archiver/auth.py) | `APIClient` + auth strategies (bearer / api-key header / api-key query / basic) |
| [archiver/feed.py](archiver/feed.py) | `Feed` dataclass bundling name + client + parser + decoder |
| [archiver/parser.py](archiver/parser.py) | `Parser` registry (`protobuf`, `json`) |
| [archiver/response.py](archiver/response.py) | response polymorphism (`FeedResponse`, `ProtobufResponse`, `JsonResponse`, `ErrorResponse`, `UnknownResponse`, `TransportErrorResponse`) |
| [archiver/writer.py](archiver/writer.py) | `LocalWriter` — the landing-zone write (atomic via tmp+rename) |
| [archiver/decoder.py](archiver/decoder.py) | `Decoder` registry + GTFS-RT entity decoders + row dataclasses |
| [archiver/rollup.py](archiver/rollup.py) | `Rollup` — landing → curated Parquet |
| [archiver/shipper.py](archiver/shipper.py) | `Shipper` — curated + landing → S3 |
| [archiver/uploader.py](archiver/uploader.py) | `Uploader` Protocol + `S3Uploader` implementation |
| [archiver/telemetry.py](archiver/telemetry.py) | `Telemetry` Protocol + `NoOpTelemetry` default |
| [archiver/telemetry_datadog.py](archiver/telemetry_datadog.py) | DogStatsD implementation (lazy-imported) |
| [archiver/summary.py](archiver/summary.py) | `summarize_feed()` — entity counts for metadata rows |

The Parser, Decoder, and Telemetry classes are deliberately Protocols / registries so a new feed shape or telemetry backend can be added without touching the poll loop.

---

## Testing

```bash
uv run pytest
```

Tests live under [tests/](tests/) and exercise the config validator, parser dispatch, decoder output, writer paths, rollup behavior, shipper key construction, and the Datadog telemetry adapter. Fakes live in [tests/fakes/](tests/fakes/) — there is no live network or S3 dependency.

---

## Observability

If `telemetry.enabled: true`, the archiver, rollup, and shipper emit StatsD metrics + spans to the configured Datadog agent. Notable metrics:

- `feed.poll` span (per feed, per poll) — tagged with `feed`
- `rollup.run` / `rollup.day` spans — tagged with `feed`, `day`
- `rollup.skipped` counter — when outputs already exist
- `ship.cold` / `ship.hot` spans, `ship.cold.bytes` / `ship.hot.bytes` histograms, `ship.cold.skipped` / `ship.hot.skipped` counters

With telemetry disabled, the `NoOpTelemetry` implementation is a true no-op — no agent required.

---

## Roadmap / known gaps

- **No retries on transport errors.** A single failed poll is logged + recorded as a `TransportErrorResponse` metadata row, and the next tick tries again. There's no backoff or per-feed circuit breaker yet.
- **No schema-drift detection on JSON decoders.** A TODO in [decoder.py](archiver/decoder.py) calls out that a MARTA API change would currently silently break parsing.
- **MTA NYCT decoder is a stub.** `MTADecoder` extends `StandardDecoder` but doesn't yet read the NYCT protobuf extensions.
- **HRT and several other agencies are disabled.** See commented blocks in [feeds.yaml](config/feeds.yaml); some endpoints are unreachable, others were never enabled.
- **Single-host poller.** Concurrency between feeds within a tick is sequential — fine for ~60 feeds at 60s, would need rethinking at higher fan-out.

---

## License

[MIT](LICENSE) © 2026 Andrew Kouré
