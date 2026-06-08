# Sharded async ingest — design sketch

> Status: design / not implemented. Target: scale from ~50 feeds (single process)
> toward the ~2,000-agency "all feeds" ceiling without the write path or the
> network model falling over.

## 1. Where we are

Today's loop is single-process and synchronous (see [`main.py`](../../main.py)):

```
Scheduler (min-heap by due_at)  ->  sleep until due  ->  Dispatcher.submit
                                                              |
                                                  ThreadPoolExecutor(10)
                                                              |
                                  FeedArchiver.archive_one  (blocking requests.get)
                                                              |
                                  LocalWriter.write  (1 .bin per poll + 1 jsonl row)
```

This is correct and observable, but three properties cap it:

1. **Thread-per-poll, blocking I/O.** A poll holds a worker thread for the full
   request RTT. At 15 s intervals, 2,000 feeds is ~133 polls/s sustained; covering
   that with blocking threads means hundreds-to-thousands of threads, all mostly
   parked on socket reads. Thread stacks and context-switching, not CPU, become the
   ceiling.
2. **One object per poll.** 133 polls/s × 86,400 s ≈ **11.5 M objects/day**, each a
   few KB. On a filesystem that's inode exhaustion; on S3 it's ~$58/day in PUT
   charges alone (`$0.005 / 1,000`) before a byte of storage, plus per-object
   overhead that dwarfs the payloads. This is the single biggest blocker.
3. **No conditional GET / no dedup.** Many GTFS-RT feeds are unchanged between
   polls (especially alerts at 60 s, and any agency mid-service-gap). We
   re-download and re-store identical bytes every time.

Plus two structural limits: one process is a single failure domain, and one box
has one network egress / CPU ceiling. And per-agency rate limits (the disabled
511 Bay Area block: 6 agencies, one 60 req/hr key) need a budget that lives
*somewhere* — today there's nowhere to put it.

## 2. Shard model

**Partition feeds across N workers by a stable hash of `agency_id`**, not of
`feed.name`.

```
shard_index(agency_id) = stable_hash(agency_id) % N
```

Why agency, not feed:

- **Rate-limit budgets are per-agency.** Keeping every feed of an agency on one
  shard makes a token-bucket limiter *local and lock-free* — no cross-process
  coordination to honor "60 req/hr for these 6 agencies." This is exactly the
  constraint that benched the 511 block.
- **Auth/session locality.** [`APIClient`](../../archiver/auth.py) already shares
  one `requests.Session` (→ connection pool) per auth group. Co-locating an
  agency's feeds keeps that pool, and the API key, on one worker.
- **Blast radius.** A misbehaving agency (redirect loop, slow TLS) degrades one
  shard, not all of them.

Assignment lives in a **control document** (start: a YAML/JSON object in the
landing bucket; `{agency_id: shard_index}` plus `shard_count`). Each worker reads
it at boot and on SIGHUP, and ingests only its assigned agencies.

- **v1 — static:** `shard_count` is fixed config; assignment is pure
  `hash % N`. Deterministic, no coordinator, trivially testable.
- **v2 — dynamic:** a coordinator rewrites the control doc to rebalance (new
  agencies, hot shards). Use **highest-random-weight (rendezvous) hashing** rather
  than plain modulo so that changing `N` reshuffles only `1/N` of agencies instead
  of nearly all of them. Workers diff the doc and pick up / drop agencies; in-flight
  polls drain first.

No shared mutable state between shards — the only shared thing is the (read-mostly)
control doc and the object store. That keeps the concurrency story boring on purpose.

## 3. Per-shard async poll loop

Replace the thread pool with **one asyncio event loop per shard** over
`aiohttp`/`httpx`. The scheduler stays a min-heap (the [`Scheduler`](../../archiver/scheduler.py)
logic is unchanged) but instead of `sleep → submit-to-pool`, the loop awaits the
next-due time and spawns a coroutine. Thousands of coroutines parked on sockets
cost almost nothing — no thread stacks.

```python
async def run_shard(feeds, control):
    sched = Scheduler(feeds, default_interval=control.default_interval)
    limiters = {a: TokenBucket(rate) for a, rate in control.agency_limits.items()}
    async with aiohttp.ClientSession() as http:
        while not shutting_down:
            due_at, feed = sched.next_due()
            await sleep_until(due_at)
            asyncio.create_task(poll_one(http, feed, limiters[feed.agency_id]))
            sched.mark_polled(feed)
```

`poll_one` is the existing `archive_one` made async, with three additions:

### Conditional GET

Keep a tiny per-feed validator cache (`{feed: {etag, last_modified}}`, persisted so
it survives restarts):

```python
headers = {}
if v := validators.get(feed.name):
    if v.etag:          headers["If-None-Match"]     = v.etag
    if v.last_modified: headers["If-Modified-Since"]  = v.last_modified

async with limiter, http.get(url, headers=headers) as resp:
    if resp.status == 304:
        record_metadata(feed, status=304, body=None)   # no body fetched
        return
    body = await resp.read()
    validators.set(feed.name, resp.headers.get("ETag"),
                              resp.headers.get("Last-Modified"))
```

A 304 means *we paid one round trip and zero bytes of body or storage*. This is the
"good citizen" lever against thousands of agencies.

### Content-hash dedup (the backstop)

Many GTFS-RT endpoints are static files served with no/var­ying validators, so they
200 every time. Hash the body and compare to the last stored hash for that feed:

```python
digest = sha256(body).hexdigest()
if digest == validators.last_digest(feed.name):
    record_metadata(feed, status=200, body=None, dedup_of=digest)  # no new object
    return
validators.set_digest(feed.name, digest)
enqueue_for_write(feed, digest, body)
```

The metadata row still gets written every poll (so "we successfully contacted the
feed at T" is always recorded), but the **payload is stored once per distinct
content**. This is most of why a real archive's effective compression ratio is so
high. It also composes with a content-addressed layout (§4): `dedup_of` points at the
object that already holds those bytes.

### Backoff, jitter, dead-feed quarantine

- Add ±jitter to `mark_polled` so feeds sharing an origin don't synchronize.
- On transport error / 5xx: exponential backoff per feed (cap, say, 10 min).
- After K consecutive failures: **quarantine** — reschedule at a long interval and
  emit an alert, but never silently drop. (The [HRT feed](../../config/feeds.yaml)
  is a commented-out dead feed today; quarantine is the automated version of that.)

## 4. Batched write path (highest leverage)

The fix for the 11.5 M-objects/day problem: **buffer payloads and flush as one object
per (feed, time-window)** instead of one per poll. This keeps the two-tier model
intact — the batched raw log *is* the landing zone; rollup still turns it into
parquet. (We are **not** replacing `LocalWriter` with a parquet writer.)

A `BatchingWriter` wraps the current [`LocalWriter`](../../archiver/writer.py):

```
poll -> BatchingWriter.add(feed, digest, body)
            buffers in memory per (feed, current_window)
        flush trigger (window elapsed | size cap | shutdown)
            -> write ONE framed object for the window
            -> append metadata rows for the window
            -> atomic tmp+rename  (or S3 multipart)
```

Two compatible storage shapes for the raw bytes:

1. **Framed append log per window.** One object per `(feed, window)` holding
   length-prefixed records: `[u32 len][digest][payload]…`. ~1 object per feed per
   window (e.g. 5 min) instead of per poll → at a 5-min window that's
   `50 feeds × 288 windows/day ≈ 14k objects/day` today, and it scales linearly with
   feeds, not with poll rate.
2. **Content-addressed blob store.** Store each *distinct* payload once at
   `raw/by-hash/<digest>.bin`; the per-day metadata jsonl carries the digest. Dedup
   (§3) then makes storage proportional to *change events*, not polls. This pairs
   best with the framed log: frames reference digests, blobs hold bytes once.

Either way the **metadata jsonl stays the index** and stays append-batched per day
exactly as it is now — that file is already the right shape.

Flush correctness:

- Flush on `min(window_elapsed, size_cap)` and **always on shutdown** (SIGTERM
  handler drains buffers before exit) so a deploy/restart can't lose a partial
  window.
- Atomic publish: tmp+rename locally (current pattern), or S3 multipart-complete —
  readers never see a half-written window.
- Bound memory: cap buffered bytes per shard; if the writer can't keep up, apply
  backpressure to the loop (skip a poll cycle) rather than OOM.

## 5. Failure & observability

This directly addresses the known monitoring gap (an 18 h outage once went
unalerted, and the Datadog monitors aren't deployed):

- **Distinguish "unchanged" from "gone."** Track per feed: `last_contact`
  (any response, incl. 304), `last_distinct_payload` (new content stored), and
  `consecutive_failures`. A feed that 304s for hours is *healthy*; a feed with no
  `last_contact` is *down*. Two different monitors.
- **Per-shard heartbeat.** Each shard emits a heartbeat metric every loop tick;
  absence ⇒ dead shard. With sharding, "the archiver is down" stops being a single
  binary and becomes "shard 3 is down" — which is also the alert.
- **Staleness monitor** on `last_contact` per feed feeds the existing
  [`monitors/`](../../monitors/) / [`dashboards/`](../../dashboards/) definitions
  that still need to be deployed.

## 6. The part engineering can't fix

Auth-key provisioning for thousands of agencies is the real cap on "all feeds." Most
require a developer-portal signup, manual approval, or per-agency T&C acceptance —
there's no API to mint keys at scale. The ingest architecture above scales to
whatever set of keys we hold; acquiring the keys is a manual/ops pipeline, not a
code problem. Worth stating plainly so it doesn't masquerade as an engineering TODO.

## 7. Migration path (each step independently shippable)

1. **Conditional GET + content-hash dedup** in the existing single process. No new
   infra; immediate bandwidth + storage win; measurable as dedup-hit rate.
2. **`BatchingWriter`** wrapping `LocalWriter`. Kills the object-count problem;
   landing-zone layout and rollup unchanged.
3. **asyncio loop** replacing the thread pool inside one process. Same scheduler,
   same feeds; removes the thread ceiling.
4. **Shard across processes/pods** by agency hash + control doc; static `N` first,
   rendezvous-hash rebalancing later.

Steps 1–2 are pure wins available *now* at current scale and don't depend on 3–4.
Steps 3–4 are "more boxes / standard patterns" — they only matter once the feed
count climbs.
