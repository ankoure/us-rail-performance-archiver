# Split poll / rollup tiers — design sketch

> Status: design / not implemented. Target: decouple the always-on poll tier from
> the daily rollup burst so the CPU-heavy batch scales independently (and cheaply)
> as the feed catalog grows, instead of forcing repeated upsizes of one box.
>
> All cost/volume figures below are measured from the prod box on 2026-06-09
> (data day 2026-06-08, 244 feeds), not estimated. Re-measure before committing —
> the feed count and per-feed volume move.

## 1. Where we are

Everything runs on one EC2 box (`i-0cea442ce8ed4d8e5`, currently t3.large: 2 vCPU /
8 GiB): the sharded pollers, the daily `batch` loop (rollup → ship → prune), and a
Datadog agent, all sharing one 500 GiB EBS volume mounted at `/opt/rail-archiver`.
See [`compose.prod.yml`](../../compose.prod.yml).

The workload is two profiles glued together on one machine:

- **Pollers are I/O-bound.** They GET, conditional-GET/dedup, and store *raw bytes*
  — the protobuf parse happens in rollup, not here. Measured: shard-0 1.5% CPU,
  shard-1 4.9% CPU. ~0.1 core combined.
- **The daily batch is a CPU burst.** `rollup.py` re-parses protobuf into parquet,
  then `ship.py` gzip-tarballs the landing zone. Measured during a real run: a
  single `ship.py` at **200% CPU for 18+ min**, load average **8.3 on 2 cores**,
  97% user / 0% idle / 0% iowait. RAM was 88% free, swap untouched — it is purely
  CPU-bound, not memory- or disk-bound.

The batch window already runs ~76 min for the long tail (a few big feeds — NYCT,
BART — roll up serially per feed) and grows linearly with feed count. The box has
been upsized once already; that upsize added RAM (4→8 GiB) the box doesn't need
while the real ceiling is the 2 vCPUs.

This is the classic split-the-tiers profile: a cheap always-on tier that does
almost no CPU, and a burst tier that wants many cores for under an hour a day.

## 2. The coupling to break: the landing zone

The only reason rollup must co-locate with the pollers is that it reads the landing
zone off the **same local EBS volume** the pollers write. Break the boxes apart and
the rollup tier needs the landing data some other way. Three substrates:

- **EBS** — can't multi-attach across instances (and detach/attach is fragile; the
  pollers need continuous write). Out.
- **EFS** — works, but ~$0.30/GiB-mo and poor/expensive for the many-small-files
  pattern the landing zone is. Out.
- **S3** — $0.023/GiB-mo, and already where the data is ultimately destined (the
  cold tarball *is* the gzipped landing zone). **This is the answer.**

So the real change is: **the landing zone becomes S3-backed.** This is a natural
extension of the two-tier design, not a rewrite — the `BatchingWriter` already
produces immutable, content-addressed window objects (~91/feed/day after in-window
dedup), which are exactly what you want to push to S3 instead of re-syncing an
appending file.

The one design swing is **metadata**, which is appended per-poll today and can't be
appended to an S3 object. Either:
- (a) one immutable metadata object per window flush (S3-native; ~doubles PUT count), or
- (b) batch metadata coarser, e.g. one object per feed per hour (fewer PUTs, the
  rollup join tolerates it).

(a) is simplest and is what the cost model below assumes; (b) is the cost lever if
PUTs matter.

## 3. Target architecture

```
cheap always-on tier                    ephemeral burst tier
┌─────────────────────┐                 ┌──────────────────────────────┐
│ poller box          │   s3://landing  │ EventBridge cron (~03:30Z)   │
│ 1× t4g.small (ARM)  │ ───windows────► │   → ECS RunTask              │
│ async loop handles  │   + metadata    │   Fargate Spot 8 vCPU/16 GiB │
│ all 244 feeds       │                 │   rollup → ship → prune       │
│ (~0.1 core)         │                 │   reads s3://landing,         │
│ ~30 GiB gp3 scratch │                 │   writes curated + cold,      │
└─────────────────────┘                 │   exits (~1 hr)               │
                                        └──────────────────────────────┘
                                          curated parquet → s3://hot
                                          cold tarball    → s3://cold (DEEP_ARCHIVE)
```

Notes:
- **One poller box, not two.** Step-3 async is I/O-bound, so a single process
  covers all feeds; sharding existed for the 2-vCPU box's parallelism, which no
  longer applies. (Keep the shard flags — they're free and forward-compatible.)
- **Scheduled ECS task on Fargate Spot**, not a spot EC2 instance: no AMI, no ASG,
  no instance lifecycle, no self-terminate script. EventBridge cron → `ECS RunTask`
  runs the same image's `rollup.py && ship.py && prune.py` against S3, then exits.
  Fargate caps at 16 vCPU / 120 GiB — fits comfortably. AWS Batch on a spot compute
  environment is the heavier fallback if you ever outgrow that ceiling.
- **Spot interruption is a non-issue.** Rollup is idempotent (ship's `exists()`
  gate); a killed run just re-runs.
- **Keep the tarball.** Don't be tempted to drop it and lifecycle the raw landing
  objects straight to DEEP_ARCHIVE — at 244 feeds that's ~22k tiny objects/day, and
  Glacier's per-object overhead would eat the savings. The tarball's job is object
  *consolidation* for Glacier economics; that survives the split, it just runs on
  rented cores where CPU is cheap.

## 4. Cost model (measured)

Measured inputs (2026-06-08, 244 feeds):

| Metric | Value | Source |
|---|---|---|
| Raw landing | 21.3 GiB/day, 22.2k objects/day | box |
| Cold archive (DEEP_ARCHIVE) | 128 GiB, 1,822 objects | CloudWatch |
| Hot parquet (Standard) | 91 GiB, 4,180 objects | CloudWatch |
| Cold daily growth | ~17 GiB/day (gzip ≈ 1.24× on protobuf) | derived |
| Hot daily growth | ~3.8 GiB/day | derived |

**Monthly run-rate, split architecture (today's feed count):**

Fixed compute + infra:

| Item | Spec | $/mo |
|---|---|---|
| Poller box | 1× t4g.small | 12 |
| Poller EBS | 30 GiB gp3 (down from 500) | 2.40 |
| Rollup | Fargate Spot 8 vCPU / 16 GiB, ~1 hr/day | 5 |
| Datadog agent | 1 host (external bill, not AWS) | ~15 |
| **Subtotal** | | **~34** |

S3 requests (the landing-zone split cost):

| Item | $/mo |
|---|---|
| Landing PUTs (windows + per-window metadata) | ~6.6 |
| Landing GETs (rollup reads) | ~0.3 |
| Transient landing storage (3-day prune buffer, ~64 GiB) | ~1.5 |
| **Subtotal** | **~8.4** |

S3 storage (now; accumulates — see §5):

| Item | $/mo now |
|---|---|
| Cold DEEP_ARCHIVE (128 GiB @ $0.00099) | 0.13 |
| Hot Standard parquet (91 GiB @ $0.023) | 2.10 |
| **Subtotal** | **~2.2** |

**Total now: ~$45/mo** (~$30 AWS + ~$15 Datadog).

Data transfer is **$0**: S3 ↔ EC2/Fargate in-region is free; feed polling is
ingress (free). The only egress would be querying hot parquet from outside AWS,
which isn't part of the pipeline run-rate.

Side-by-side:

| | Current (one t3.large) | Split (now) |
|---|---|---|
| Compute | t3.large 24/7 ~$61 | t4g.small $12 + Fargate Spot $5 |
| EBS | 500 GiB ~$40 | 30 GiB ~$2.4 |
| S3 requests | minimal | ~$8.4 |
| S3 storage | ~$2.2 (same) | ~$2.2 (same) |
| Datadog | ~$15 | ~$15 |
| **AWS subtotal** | **~$105/mo** | **~$30/mo** |

The split roughly **thirds the AWS run-rate**. The two structural wins are the
**EBS collapse** (500→30 GiB) and **renting rollup cores by the minute**; the new
~$8/mo of S3 requests is small against both. More important than the dollar saving:
the CPU burst is decoupled from the always-on box, so onboarding the rest of the
catalog never forces another upsize.

## 5. Going forward: storage is the real driver

Compute is flat; **storage grows linearly forever**, and hot Standard parquet is
the long-term cost — it dwarfs both compute and the split's S3 requests within a
year. This is architecture-independent (same in today's setup); it's just the
honest "going forward" number.

| | Cold (DEEP_ARCHIVE, +17 GiB/day) | Hot (Standard, +3.8 GiB/day) | Storage $/mo |
|---|---|---|---|
| Now | 128 GiB → $0.13 | 91 GiB → $2.10 | ~2.2 |
| +1 year | ~6.3 TiB → $6.3 | ~1.5 TiB → **$34** | ~40 |
| +2 years | ~12.5 TiB → $12.5 | ~2.8 TiB → **$65** | ~78 |

**The lever: lifecycle-tier hot parquet you aren't actively querying.** Standard →
Glacier Instant Retrieval ($0.004/GiB, still millisecond reads) after ~90 days cuts
the aged tail ~6×. Applied to the +2yr case, hot drops from ~$65 to ~$15/mo — a
single lifecycle rule saves more long-term than the entire compute split does.
(Per-object overhead stays negligible because parquet is ~174 objects/day, not 22k.)

## 6. Migration path (each step independently shippable)

1. **S3-backed landing write.** ✅ *Done 2026-06-10.* Implemented as a `Sink` seam
   (`archiver/sink.py`: `LocalSink`/`S3Sink`/`TeeSink`) injected into `BatchingWriter`
   — not a `BaseWriter` subclass — so the writer just frames+names and hands bytes to
   a destination. Window `.bin` and a per-window metadata object (`window=*.jsonl`,
   strategy 2a) both route through the sink; daily `data.jsonl` stays local so the
   rollup is non-breaking. `dual` mode tees local+S3; live dry-run parity verified.
2. **Rollup reads S3.** ✅ *Done 2026-06-10 (rollup).* Read-side `Source` seam
   (`archiver/source.py`: `LocalSource`/`S3Source`) injected into `Rollup`, mirroring
   the `Sink`. `read_metadata` hides the asymmetry (local daily `data.jsonl` vs N S3
   `window=*.jsonl` concatenated sorted-by-window). `rollup_source: local|s3` config
   knob via `build_source`. **Golden parquet parity verified** — S3-landing rollup is
   byte-identical to the local-tree rollup. `ship.py` `_discover` swap deferred to
   step 4 (its cold-tar + prune assume a local landing).
3. **Fargate Spot rollup task.** EventBridge cron → ECS RunTask on Fargate Spot,
   same image, env-configured to S3 mode. Run it alongside the on-box batch for a
   few days and diff outputs before cutting over.
4. **Shrink the poller box.** Move pollers to t4g.small, drop EBS to a small scratch
   volume, delete the on-box `batch` service. The landing zone now lives in S3.
5. **Lifecycle rule on hot** (§5) — independent of the split, do it whenever; it's
   the biggest long-term lever.

## 7. Risks & open questions

- **Rollup-after-upload ordering.** The Fargate task must run after the last of
  yesterday's windows has uploaded. Windows close every 5 min and upload promptly,
  so a 03:30Z run for yesterday-UTC has hours of margin — but it's the one
  correctness knob to monitor (a "landing complete" marker per day would make it
  explicit).
- **Fargate task sizing.** The ~1 hr / 8 vCPU assumption rests on the big-feed tail
  (NYCT/BART roll up serially per feed) staying under an hour. 8 vCPU only helps
  when ≥8 feeds roll concurrently; the tail is a few big serial feeds, so wall-clock
  may be dominated by the single largest feed regardless of core count. Measure a
  real run before fixing the task size.
- **Durability of the open window.** Batching holds up to one ~5-min window in the
  poller's RAM; a hard crash (SIGKILL/OOM/power) loses it, graceful SIGTERM drains
  it (already proven). Unchanged by the split, but the S3 upload adds a second
  failure point (upload lag) — size the scratch buffer to survive an S3 outage.
- **Datadog billing for ephemeral Fargate.** May add a small per-container charge
  depending on plan; confirm before assuming the ~$15 stays flat.
- **Metadata object strategy** (§2 a vs b) — pick before step 1; it sets the PUT
  cost and the rollup join shape.

## 8. Decoupling the landing upload (outbox)

> Status: designed 2026-06-11, **not yet built**. Supersedes step-1's synchronous
> `S3Sink`-in-`TeeSink` for the `dual`/`s3` write path, and resolves the §7
> "size the scratch buffer to survive an S3 outage" risk.

**Problem (measured).** In `landing_mode: dual` the `BatchingWriter` flush writes S3
inline: `_write_buckets` calls `TeeSink.put` sequentially per object, and each S3 leg
is a blocking boto3 PUT. The flush is `await`ed on the poll dispatch loop (`main.py`,
fired on the 300 s window-index change), so the loop freezes for the whole flush.
Measured on prod 2026-06-11: ~150 sequential PUTs/shard ⇒ **14–20 s of blocked
dispatch every window**, on both shards, exactly at the wall-clock boundary. The
stall bunches the schedule; reschedule jitter takes ~150 s to re-disperse, so
`poll.skipped` stays elevated for ~half of every window (the ~208/15min the "Ingest
saturated" monitor fired on). Confirmed three independent ways: code path →
`.heartbeat` staleness (14 s/19 s peaks at the boundary, both shards) → DogStatsd UDP
capture (every `s3.request:put` and every `poll.skipped` landed within 20 s of the
one boundary, zero elsewhere). **Not a capacity problem** — `--workers` only masks it.

**Decision: an outbox.** The S3 write leaves the flush entirely.

- The poller flush writes **local only** (`LocalSink`). A local-only flush is tens of
  ms, so the dispatch stall disappears immediately — before any S3 work happens.
- **Disk *is* the queue.** A separate long-running `LandingUploader` (a peer service,
  *not* a `Sink` — "watch a dir and drain it" isn't a `put`) scans the landing dir,
  ships each object via the existing sync `InstrumentedUploader.upload`, and deletes
  on success (end state only). Pending == present-on-disk, so restart recovery is just
  a boot scan and a write is **never lost** (durable on disk before we ever touch S3).
- **Plain worker thread** — not an event-loop coroutine (boto3 blocks → would
  re-freeze the loop) and not `aioboto3` (buys concurrency this throughput, ~1.5
  PUT/s steady, doesn't need, while splitting the S3 layer in two). Runs off the loop,
  shares only the disk, reuses the `s3.request` instrumentation.

**Why it's safe, and its scope.** Enqueue-and-return weakens `put`'s durability
postcondition — valid only because another authoritative copy exists. The uploader is a
**single-consumer** drainer: it owns the local landing and drains it by deleting on
ship, so "present on disk" means "unshipped." That model holds only with one reader —
see "Collapsing `dual`" for why continuous dual-write was dropped. During the migration
soak the poller runs `local` (local is authoritative; S3 is populated out-of-band by the
backfill job); at cutover (`s3`, §3) S3 becomes authoritative and local is 30 GiB
scratch, so the outbox must **never drop** — the disk buffer absorbs an S3 outage
(~1.4 days at 30 GiB / 21 GiB-day) and the alarms below are the safety net.

**Config mapping.** `landing_mode` is `{local, s3}` (the old continuous `dual` is
collapsed — see below):

| mode | flush sink | uploader |
|---|---|---|
| `local` | `LocalSink` | — |
| `s3` | `LocalSink` | `LandingUploader` (single consumer; **always** deletes on ship) |

There is **no `delete_after_ship` flag**: the uploader is only ever wired as a single
consumer, so deleting on ship is its defining invariant — made structural rather than
configurable (a `False` setting would re-ship every resident object every scan, a money
fire). Wiring: `build_sink` returns a bare `LocalSink` for `s3` (not `S3Sink`);
`build_writer` constructs the `LandingUploader` when `landing_mode == "s3"`; `main.run`
enters it into the `AsyncExitStack`.

**Collapsing `dual`.** Continuous dual-write (flush tees local+S3) is dropped: with the
async outbox, local and S3 become *two consumers* of the landing dir and neither can
delete (each may still need the file), so disk-as-queue breaks — every resident object
re-ships every scan. Instead, S3 parity during the soak is verified by a separate
**one-shot, `exists()`-gated landing-backfill job** (reusing `ship.py`'s idempotency
idiom at batch cadence, where per-object HEADs are cheap): walk local windows, skip what
is already in S3, upload the rest, diff. Run it periodically through the soak for ongoing
parity, then flip `local`→`s3` at cutover. The backfill is a separate script, **not**
part of `LandingUploader` — which stays a dumb single-consumer drainer. (This supersedes
§6 step 1's continuous-`dual` mechanism.)

**Lifecycle (RAII).** `LandingUploader` is an async context manager entered into the
poller's `AsyncExitStack` alongside the agency clients: start the thread on enter,
signal-stop + finish-current-PUT + join on exit. The stack unwinds *after* the loop's
`finally` (drain inflight → `flush_all`), so the last windows are on disk before the
uploader's final drain runs — correct ordering for free. Shutdown leaves un-shipped
files for the next boot's scan (safe, durable); an optional bounded drain ships what
it can first.

**Scan cadence.** A plain fixed interval (~30 s), decoupled from the flush. Latency is
irrelevant (Fargate rollup runs daily — hours of margin), and the interval has
negligible effect on disk: when uploads keep up the on-disk backlog is
`interval × 0.25 MiB/s` (~7 MiB at 30 s, vs 30 GiB scratch); when they *don't*, disk
fills at the landing rate regardless of how often you scan. So the interval is a
simplicity knob, not a safety knob. An optional `threading.Event` set by the flush can
wake the scan sooner if low ship-latency is ever wanted.

**Observability.** Two layers: a **`landing.pending` gauge** (count of un-shipped
objects — the *leading* indicator; backlog climbs long before disk fills if S3 stalls)
plus oldest-pending age for lag; and a **disk-capacity alarm** as the *lagging*
backstop. These would be the first monitors actually deployed (today the Datadog
monitors aren't live and the notify target is a placeholder — an alert that never
pages is the failure mode to avoid; the box has wedged on disk/IO once already). Also
closes the gap that `s3.request` is counted but never timed, so flush/upload health
was previously invisible.

**Atomic writes** are already satisfied — `LocalSink.put` writes `*.tmp` then
`rename`s (`archiver/sink.py`), so the scanner never sees a partial object.

**Open implementation question.** The scan must select only *shippable* window objects
(`raw/*.bin`, `metadata/window=*.jsonl`) and skip local-only artifacts (the daily
`data.jsonl` the on-box rollup reads, plus any `*.tmp`). Simplest is to glob the
window-object key pattern; a dedicated pending subtree is the cleaner-but-more-invasive
alternative. Decide before building `_scan_once`.
