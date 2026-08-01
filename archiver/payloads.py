"""Format-agnostic extraction of individual poll payloads from a raw .bin file.

Shared by archiver.rollup.Rollup (curated silver) and analysis.alert_snapshot
(daily alert dedup) — both need "give me each poll's payload bytes and its
true fetched_at timestamp" from whatever on-disk/S3 shape landing happens to
be in. Pulled out of Rollup once a second caller needed it; Rollup._rollup_data
calls these same functions rather than duplicating the logic.
"""

from __future__ import annotations

import io
import json
from datetime import date
from typing import Iterator

from archiver.logger import logger
from archiver.source import Source
from archiver.writer import FrameError, FrameReader


def digest_timestamps(source: Source, feed_name: str, day: date) -> dict[str, int]:
    """Map each stored payload's content digest -> its earliest poll timestamp.

    Built from the day's metadata jsonl (the index): every poll writes a row with
    `timestamp` and `digest`, *including* dedup'd / 304 polls that stored no frame.
    A framed window object only holds DISTINCT payloads, so each frame's digest
    joins here to recover the true per-poll `fetched_at` that the `window=<unix>`
    filename can't carry. If a digest appears on several rows (rare intra-window
    content flap A->B->A collapses to one frame but leaves two rows), keep the
    EARLIEST timestamp.

    Returns {} if the day's metadata file is absent (e.g. a raw-only partition).
    """
    data = source.read_metadata(feed_name, day)
    if not data:
        return {}
    lines = data.decode().splitlines()
    digest_timestamps: dict[str, int] = {}
    for lineno, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue

        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Skipping malformed metadata row %s:%d: %s",
                feed_name,
                day,
                lineno,
                exc,
            )
            continue

        digest = row.get("digest")
        raw_ts = row.get("timestamp")

        # A digest-less row is expected, not malformed: transport-error and
        # other non-payload rows carry no digest (nothing was stored), so they
        # are simply not join candidates. Skip silently — warning here turned a
        # routine feed outage (e.g. a DNS blip) into WARNING-level log spam.
        if digest is None or raw_ts is None:
            continue

        # Match legacy `int(float(stem))` coercion used when timestamps
        # were embedded in window filenames, so Phase C golden parquet parity holds.
        ts = int(float(raw_ts))

        if digest not in digest_timestamps or ts < digest_timestamps[digest]:
            digest_timestamps[digest] = ts

    return digest_timestamps


def iter_payloads(
    name: str, data: bytes, digest_ts: dict[str, int]
) -> Iterator[tuple[bytes, int]]:
    """Yield (payload_bytes, fetched_at) for one raw .bin file, format-agnostic.

    Three on-disk shapes coexist:
      * legacy LocalWriter  -> filename stem IS the wall-clock ts; the whole file
        is ONE payload.
      * BatchingWriter      -> filename `window=<unix>`; the file is `\\x89GRT` +
        N framed payloads. `FrameReader` yields (payload, raw-digest-bytes); the
        metadata digest is a hex string, so `.hex()` the frame digest to join into
        `digest_ts`. Fallback when a digest is missing: the window-start unix in
        the stem (coarse, but never crashes).
      * Hourly merged       -> filename `hour=<unix>`; same framed format as
        `window=`, produced by LandingUploader when `merge_to_hourly=True`. The
        hour-start unix is used as the fallback timestamp when a digest is absent.

    Keeping this the single source of "how to get payloads out of a file" lets
    every caller (Rollup's parse -> decode -> append loop, alert_snapshot's
    parse -> merge loop) stay format-agnostic.
    """
    stem = name.removesuffix(".bin")

    if stem.startswith("window=") or stem.startswith("hour="):
        # --- BatchingWriter framed file (per-window or hourly-merged) ---
        # Stem is "window=<unix>" or "hour=<unix>"; parse the fallback timestamp.
        try:
            fallback_ts = int(stem.split("=", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"Cannot parse timestamp from filename {name!r}") from exc

        try:
            reader = FrameReader(io.BytesIO(data))
            for payload, raw_digest in reader:
                fetched_at = digest_ts.get(raw_digest.hex(), fallback_ts)
                yield payload, fetched_at
        except (FrameError, EOFError) as exc:
            logger.warning(
                "Truncated/corrupt frame in %s (fallback_ts=%d); skipping remainder: %s",
                name,
                fallback_ts,
                exc,
            )

    else:
        # --- Legacy LocalWriter file ---
        # The entire file is one payload; the stem IS the wall-clock timestamp.
        try:
            fetched_at = int(float(stem))
        except ValueError as exc:
            raise ValueError(
                f"Cannot parse legacy timestamp from filename {name!r}"
            ) from exc

        yield data, fetched_at
