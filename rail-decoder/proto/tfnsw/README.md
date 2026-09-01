# TfNSW GTFS-R schema

Transport for New South Wales publishes its own permutation of GTFS-realtime,
not an overlay on the canonical spec. It is treated here as a separate schema
with its own package (`tfnsw_realtime`), compiled alongside — never merged
with — `proto/gtfs-realtime.proto`.

## Files

- `UPSTREAM.proto` — pristine, byte-for-byte as published. **Never built.**
  Exists only so the next TfNSW release can be diffed against it.

      curl -sSL <source url below> | diff - UPSTREAM.proto   # should be a no-op

  Source: https://opendata.transport.nsw.gov.au/sites/default/files/2023-08/gtfs-realtime_1007_extension.proto__0.txt
  Published: 2023-08 (per the portal's own path segment)
  Retrieved: 2026-09-01
  md5: `42b201365630b63784230b5ecd869590`  (8148 bytes, CRLF)

  Note: this file has CRLF line endings, and `.gitattributes` marks it `-text`
  so git will not normalise them. An earlier vendored copy — downloaded through
  a browser as `gtfs-realtime_1007_extension.proto__1.proto` — had been
  LF-normalised in transit (7845 bytes, md5
  `c4ab904dc0a9c36f593959bb8f8a1f6a`). It was otherwise identical: the whole
  303-byte delta was 302 CR bytes plus a trailing blank line. It was replaced
  with the true upstream bytes, because an LF-normalised "pristine" copy makes
  the next-release diff show every line as changed.

- `UPSTREAM-carriage.proto` — a second, older TfNSW schema. **Never built, and
  deliberately not ported.** Vendored only so this analysis isn't repeated.

      Source: https://opendata.transport.nsw.gov.au/sites/default/files/2023-08/tfnsw-gtfs-realtime-carriage.proto_.txt
      Published: 2023-08   Retrieved: 2026-09-01
      md5: `5f1cb9a745eed68a76761972b82f171d`  (2959 bytes, CRLF)

  Unlike `UPSTREAM.proto` this really *is* an extension file — it `import`s
  `gtfs-realtime.proto` and contains one `extend` block. It declares
  `TfnswCarriageDescriptor`, which is a strict subset of our
  `CarriageDescriptor`: fields 1-6 and all enum values are identical
  (15 name→number pairs match exactly), and ours adds only
  `departure_occupancy_status = 7`. So `tfnsw-realtime.proto` already decodes
  everything this file describes.

  It is also **wrong about cardinality**, which is the reason not to prefer it:
  it declares `optional TfnswCarriageDescriptor consist = 1007` (singular),
  where the newer schema has `repeated CarriageDescriptor consist = 1007`.
  A singular field keeps only the last occurrence, so this file silently drops
  carriages on any multi-carriage vehicle. Verified against live data: Inner
  West light rail reports 2 carriages on all 14 vehicles, and Sydney Trains
  reports 6-8 across 246 — this schema would keep one of each.

- `tfnsw-realtime.proto` — the build target. A *derived* artifact: same field
  numbers and enum values as `UPSTREAM.proto`, with the edits listed below.
  Do not diff this against a TfNSW release; diff `UPSTREAM.proto`.

## Edits applied to produce `tfnsw-realtime.proto`

1. `package transit_realtime` → `package tfnsw_realtime`. Two files sharing a
   package name both generate `$OUT_DIR/transit_realtime.rs`, and
   `src/transit_realtime.rs` `include!`s exactly that path. Renaming also means
   the generated Rust types have distinct *names*, not just distinct module
   paths — see the field-6 divergence below for why that matters.

2. The five `extend` blocks were deleted and their fields moved inline into the
   message bodies at the same field number (1007). prost-build has no proto2
   extension support whatsoever — `extension`, `extension_range` and
   `is_extension` appear zero times in its codegen — so every `extend` field is
   silently dropped. Inlining is the only way to decode them.

3. `extensions 1000 to 1999` ranges were dropped. They are unusable by prost,
   and on the five messages above they would collide with the now-inline 1007
   field.

4. `option java_package` dropped (no Java consumer here).

## Divergences from canonical GTFS-RT

The reason this is a permutation rather than an extension:

- **`StopTimeUpdate` field 6 conflicts on the wire.** TfNSW uses 6 for
  `departure_occupancy_status` (enum → varint). Canonical uses 6 for
  `stop_time_properties` (submessage → length-delimited). Canonical upstream
  put `departure_occupancy_status` at 7. A canonical parser reading a TfNSW
  payload hits a wire-type mismatch and drops the field to unknown fields — it
  cannot ever read this value correctly.
- `TripDescriptor.ScheduleRelationship` keeps `REPLACEMENT = 5` (dropped from
  canonical) and lacks `DUPLICATED = 6`.
- `OccupancyStatus` stops at `NOT_ACCEPTING_PASSENGERS = 6`; canonical adds
  `NO_DATA_AVAILABLE = 7` and `NOT_BOARDABLE = 8`.
- No `VehiclePosition.occupancy_percentage`, no
  `VehiclePosition.multi_carriage_details`, no `TripUpdate.trip_properties`,
  no `StopTimeUpdate.stop_time_properties`.
- Carriage-level data is carried by the 1007 `CarriageDescriptor` instead, on
  **two** streams with different grain: `VehiclePosition.consist` (observed
  now) and `StopTimeUpdate.carriage_seq_predictive_occupancy` (predicted at a
  future stop).
