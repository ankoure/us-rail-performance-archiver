# GTFS-rt curated schema drift (trip_updates / vehicles)

## Summary

The `curated/trip_updates/` and `curated/vehicles/` parquet datasets were
written with **three different column-naming conventions** over their lifetime.
The drift is **temporal, not per-feed** — the same feed (e.g. `bart-trips`)
appears in all three conventions on different days, because the writer's
column-flattening logic changed twice. `curated/alerts/` is **not** affected
(single consistent schema).

This breaks any whole-dataset read: pyarrow/pandas/DuckDB see incompatible
schemas across partitions and either error or produce mostly-null columns.

## The three eras

| Era      | Dates (observed)        | Naming style                          | Example column                                         |
| -------- | ----------------------- | ------------------------------------- | ------------------------------------------------------ |
| `FLAT`   | through **2026-05-16**  | flat snake_case                       | `arrival_delay`, `vehicle_id`                          |
| `HYBRID` | **2026-05-19 .. 05-25** | half-applied mix of flat + dotted     | `arrival_delay` *and* `trip_update.trip.trip_id`       |
| `DOTTED` | **2026-05-30 onward**   | full protobuf dotted paths            | `trip_update.stop_time_update.arrival.delay`           |

(Dates from `bart-trips`; the 05-17/05-18 and 05-26..05-29 gaps are missing
days, not boundaries.) `DOTTED` is the **current and majority** convention and
is the canonical target.

### File distribution (local snapshot, 2026-06-19)

| Dataset        | FLAT | HYBRID | DOTTED (canonical) | total |
| -------------- | ---: | -----: | -----------------: | ----: |
| `trip_updates` |  182 |    200 |               1158 |  1540 |
| `vehicles`     |  167 |    186 |               1122 |  1475 |

## What differs

Two independent problems:

1. **Names.** Flat names map 1:1 to dotted protobuf paths. `HYBRID` files use
   the *same* flat names for the subset of columns that weren't dotted yet, so a
   single flat→dotted rename map covers both `FLAT` and `HYBRID`.

2. **Missing columns.** The `FLAT` era never wrote some columns the later eras
   carry. Renaming can't recover data that was never persisted, so the migration
   adds them as **typed nulls** — the column exists (so reads unify) but the
   flat-era rows are null there:

   - `trip_updates` `FLAT` is missing: `trip_update.timestamp` (`int64`).
   - `vehicles` `FLAT` is missing: `vehicle.trip.start_time` (`string`),
     `vehicle.trip.revenue` (`bool`), `vehicle.vehicle.consist`
     (`list<struct<label:string>>`), `vehicle.multi_carriage_details`
     (`list<struct<label:string>>`).

**No type drift:** every column shared across eras has identical Arrow types, so
the migration is a pure rename + null-pad. No casts change values.

## Canonical schemas

The authoritative DOTTED schemas and the flat→dotted rename maps live in
[`scripts/migrate_rt_schema.py`](../../scripts/migrate_rt_schema.py)
(`TRIP_UPDATES_SCHEMA` / `VEHICLES_SCHEMA` and the `*_RENAME` dicts). That script
is the single source of truth — update it there, not here.

## Migration

```bash
# preview (dry run, default)
uv run python scripts/migrate_rt_schema.py

# rewrite FLAT + HYBRID files in place to the DOTTED schema
uv run python scripts/migrate_rt_schema.py --apply
```

The migration is **idempotent** (already-DOTTED files are skipped) and writes
atomically (temp file + `os.replace`). It rewrites the local `curated/` tree;
re-uploading to S3 is a separate step.

## Root cause / follow-up

- The naming convention is set by the rollup/flattening step (see `rollup.py`
  and the gold layer). The two cutovers (~05-17 and ~05-26) should be traced
  there so new writes stay on the DOTTED convention and this doesn't recur.
- The flat-era missing columns (`trip_update.timestamp`, vehicle
  `revenue`/`consist`/`multi_carriage_details`/`start_time`) are permanently
  null for pre-05-17 data — analyses depending on them must tolerate that gap.
