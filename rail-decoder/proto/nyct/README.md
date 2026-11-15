# NYCT subway extensions

NYCT's subway feeds are canonical GTFS-realtime plus proto2 `extend` fields at
tag 1001 on `FeedHeader`, `TripDescriptor` and `StopTimeUpdate`. The standard
fields NYCT publishes alongside them are placeholders: `direction_id` is always
0, and there is no `VehicleDescriptor`. The real values are only in the
extensions.

## Files

- `../nyct-subway.proto` is the pristine upstream file. **It is never built by
  prost.** It stays where it is, and keeps its name, so it can be diffed against
  an MTA release and so `protoc` can still generate the Python bindings from it
  (`archiver/nyct_subway_pb2.py`; the regeneration command is in that file's
  header).

  Source: https://groups.google.com/d/msg/gtfs-realtime/KWJhkXH0kBg/8rlRMxiL-ysJ
  md5: `53225b690c145dd1141267420f47ee64` (5230 bytes)

- `nyct-overlay.proto` is the build target. It is *derived*: it has the same
  field numbers and enum values as upstream, with the edits listed below.

## Why an overlay and not a full copy (cf. `../tfnsw/`)

prost-build has no proto2 extension support, so an `extend` field is dropped as
an unknown field. TfNSW solved this by copying all of `gtfs-realtime.proto` into
its own package with the extensions inlined. That was justified there because
TfNSW also diverges from canonical on the wire (`StopTimeUpdate` field 6), so it
needed its own decode path anyway.

NYCT does not diverge. Every canonical field decodes correctly with
`transit_realtime`, so `src/nyct/mod.rs` decodes each payload **twice**: once
canonically, through the unchanged `vehicle`/`trip_update`/`alert` builders,
and once with this overlay. The overlay declares only the path to each 1001
field, and prost skips everything else. Both decodes read the same bytes, so
entity *i* and stop-time-update *j* line up by position. The overlay walk
mirrors the canonical one's `vehicle`-before-`trip_update` precedence, and the
merge checks that row counts agree.

## Edits applied to produce `nyct-overlay.proto`

1. `package transit_realtime` → `package nyct_realtime`, so the generated code
   lands in `$OUT_DIR/nyct_realtime.rs` and doesn't overwrite the canonical
   module.
2. The `import "gtfs-realtime.proto"` was dropped. Path messages (`FeedMessage`,
   `FeedHeader`, `FeedEntity`, `TripUpdate`, `StopTimeUpdate`,
   `VehiclePosition`, `TripDescriptor`, `TimeRange`) were declared locally with
   only the fields on the path, at their canonical numbers.
3. The three `extend` blocks were deleted. Their fields were moved inline into
   the path messages at tag 1001.
4. `NyctFeedHeader.nyct_subway_version` was relaxed from `required` to
   `optional`.
5. `option java_package` was dropped, along with the comments. Read upstream
   for field semantics.
