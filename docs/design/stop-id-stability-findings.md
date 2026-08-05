# Do GTFS stop_ids stay stable across years?

> Status: research complete (Phase 4 of the speed-dashboard initiative). Not
> pipeline code — no new marts were added. The diff tool itself is a reusable
> script, `scripts/stop_id_stability.py`, built on
> `analysis/gtfs_fetcher.fetch_catalog`/`GtfsResolver`, which already existed.

## Question

Can the same GTFS `stop_id` be trusted to represent the same physical station
across multiple years, or does it drift — matters for showing any metric's
history over a multi-year timescale without silently splicing together two
different physical locations under one identifier.

## Method

`scripts/stop_id_stability.py` pulls the full archived-feeds catalog (via
the existing `analysis/gtfs_fetcher.fetch_catalog`) for a feed and diffs
`stops.txt` between two snapshots (defaults to the catalog's earliest and
latest if no dates are given). Ran it for three already-onboarded rail
agencies:

```
uv run python scripts/stop_id_stability.py --feed-id mdb-437 --agency mbta --parent-prefix place-
uv run python scripts/stop_id_stability.py --feed-id mdb-503 --agency septa_rail
uv run python scripts/stop_id_stability.py --feed-id mdb-1847 --agency wmata
```

| Agency | Catalog depth | Snapshots compared |
|---|---|---|
| MBTA (mdb-437) | 191 versions, 2024-04-25 → 2026-08-04 (~2.25 yr) | 2024-05-06 → 2026-08-04 |
| SEPTA Rail (mdb-503) | 44 versions, 2024-06-30 → 2026-08-09 (~2 yr) | 2024-07-29 → 2026-07-24 |
| WMATA (mdb-1847) | 147 versions, 2024-10-29 → 2026-07-29 (~1.75 yr) | 2024-10-31 → 2026-07-16 |

**Caveat on depth:** the MobilityDatabase archive only goes back to when it
started tracking each feed (2024 for all three here), not to when the feed
itself began. This research can only speak to ~2-year stability, not
multi-decade. It also covers 3 agencies, not a representative sample of all
GTFS producers.

For each pair of snapshots: diffed the `stop_id` set (added/removed/stable),
and for ids present in both, checked whether `stop_name` changed and whether
`stop_lat`/`stop_lon` moved by more than ~50m. Also compared `parent_station`
stability where available (required widening `StaticGtfs.stops` to expose it
— a small, permanent addition since it's a standard, broadly useful GTFS
field, not a throwaway hack).

## Findings

**Station-level identity is very stable. Platform-level churn is real but
concentrated, and most of what looks like "renaming" is bulk cosmetic
convention changes, not per-station identity drift.**

- **MBTA `place-*` station ids: 269 → 276, 100% of the original 269 persisted,
  0 removed.** All 7 new ids correspond to newly-opened facilities (distinct
  prefixes matching new commuter rail infill stations). This is the
  cleanest, most decision-relevant number: **the "station" identifier the
  dashboard would actually want for multi-year continuity is completely
  stable over the full observed window.**
- **MBTA platform-level ids (children of `place-*`, e.g. `70061`,
  `Alewife-01`): 98.4% persisted** (2763/2809). But the raw "renamed" count
  (316 of 2763) is almost entirely `door-*` ids (station entrances,
  elevators, stairs — not boarding platforms) that lost a redundant
  `"Station Name - "` prefix MBTA-wide at some point (e.g. `"Downtown
  Crossing - Temple Place, Lafaytette Place"` → `"Temple Place, Lafaytette
  Place"`) — a system-wide labeling-convention change, not per-door identity
  churn. The real passenger-facing platform ids in the "moved" list (e.g.
  `70061`/`70067`/`70068`, `Alewife-01`/`Alewife-02`) show **unchanged
  names** — the >50m "move" is a coordinate-precision refinement (rough
  station-centroid coordinate replaced by a surveyed per-platform one), not
  an actual relocation.
- **Genuine platform-id churn exists but is targeted, not random**: the
  "removed" set includes old pathway-node numbering (`102`-`104`, `800`s,
  `891`, `897`, `898`) superseded by a new numbering scheme, and several old
  Worcester Line (`WML-*`) and Middleborough Line (`MM-*`) track/platform ids
  — consistent with real renovation/renumbering projects at specific
  stations, not systemic instability.
- **SEPTA Rail: perfectly closed stop_id set (156 → 156, 0 added, 0
  removed)** over the full ~2-year window. The 25 "renamed" cases are all
  cosmetic (`"Jenkintown Wyncote"` → `"Jenkintown-Wyncote"`, adding a
  `"Transit Center"` suffix, abbreviating `"Avenue"` → `"Av"`) — no evidence
  of identity reuse.
- **WMATA's apparent 99.9% "renamed" rate is a single bulk formatting
  change** (ALL CAPS → Title Case across the entire stops.txt at some point),
  not per-station renaming — this would have been a badly misleading
  headline number without sampling the actual diffs. The genuine
  "renamed-and-moved" signal (the strongest proxy for real id recycling) is
  **2 cases total, both at the same station** (Shaw-Howard entrance
  descriptions swapped/corrected), not evidence of ids being reused for
  unrelated physical locations.
- Across all three agencies, the "renamed AND moved >50m" intersection — the
  strongest available signal for genuine id recycling to a different
  physical location — never turned up a real example. Every sampled case was
  explainable as a label correction, convention change, or coordinate
  precision fix.

## Answer to the original hypothesis

**Yes, for the timeframe this data can actually speak to (~2 years): a
station-level identifier is safe to treat as durable.** MBTA's `place-*`
parent stations and SEPTA Rail's entire stop_id set showed zero identity
churn. Platform-level ids are also quite stable (~98%) but can get
renumbered during real infrastructure projects at specific stations — this
is rare and tied to visible construction activity, not something that
happens quietly and at random.

## Recommendation for Phase 5

**Do not build a general stop_id-to-canonical-station crosswalk system.**
The problem it would solve barely exists at the timescale we can observe:
`parent_station` already provides a stable station-level identifier for free
in every feed that populates it (which includes all three agencies checked
here). A fuzzy-matching crosswalk with a manual override table — the
original Phase 5 scope — would be solving for churn that isn't actually
happening.

**What's actually worth doing, much smaller than originally scoped:**
1. Wherever the dashboard needs a "this station over multiple years" view,
   join through `parent_station` (now exposed via `StaticGtfs.stops`) instead
   of raw `stop_id`. No crosswalk infrastructure needed.
2. For feeds without `parent_station` populated, there's no cheap fix
   available from this research — flag it as a known gap if/when a
   non-MBTA-style feed needs multi-year station continuity, rather than
   building speculative fallback logic now.
3. `scripts/stop_id_stability.py` is there for the next agency check — run it
   before onboarding relies on multi-year stop_id continuity for a new feed.
   It reports the renamed-and-moved intersection specifically (not just a
   raw "renamed" count) for exactly this reason: a large renamed-only total
   with few/no renamed-and-moved cases usually means a bulk
   naming-convention change (WMATA's case), not real identity churn.
4. The default endpoint-only diff is structurally blind to flapping (a
   stop_id removed and re-added, or renamed and reverted, between the two
   dates looks perfectly stable). `--walk` loads every intermediate
   snapshot instead and catches this. Heavier to run (one download+parse per
   catalog version — 191 for MBTA), so it's opt-in, not the default.

   Ran it on both agencies and it earns its keep:
   - **SEPTA Rail**: the endpoint diff showed a clean, closed 156-id set, but
     `--walk` found **158 ids that flapped in and back out again within a
     single snapshot window** — a batch of short non-`90xxx` codes (e.g.
     `GVSO`/`WECI`/`VALLEY`), unrelated to SEPTA's normal numbering and
     plausibly a temporary producer-side data error — plus several stations
     toggling between name variants multiple times (e.g. `Trenton` <->
     `Trenton Transit Center`).
   - **MBTA** (191 snapshots, 10,602 distinct ids ever seen): only 86 ids
     (<1%) flapped, and only 4 names / 3 coordinates ever genuinely
     reverted — this *confirms* the endpoint-diff conclusion rather than
     overturning it. Flapping is concentrated in the same already-flagged
     churny spots (Worcester Line track/platform ids, pathway `node-*` ids
     at stations under construction), not spread across the system. One
     genuine anomaly worth a follow-up if this ever matters operationally:
     `FB-0143-25618-B0`/`B1` flapped **seven times** across the walked
     window — a real producer-side data-quality issue on that specific stop
     pair, not something either analysis mode would surface as a one-line
     summary without `--walk`.
