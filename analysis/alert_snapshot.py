"""Daily alerts snapshot — TransitMatters-style.

Aggregates every GTFS-RT alert appearing in a (feed, day)'s raw .bin polls into a
single dict keyed by alert v3 ID. Each value is the alert's protobuf rendered as
a dict via MessageToDict, preserving fields the curated `alerts` parquet drops:
active_period[], all language translations, the full informed_entity[] array.

Last-write-wins: the alert body for each ID is whichever appeared in the latest
poll of the day. first_seen / last_seen / poll_count are derived during the merge.

Storage shape:
    <base_dir>/snapshots/alerts/feed=<feed>/year=YYYY/month=M/day=D/data.json.gz

Note on dates: the day argument is the UTC partition day (matching how raw .bin
files are written by archiver.writer.LocalWriter), not a local service date.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from pathlib import Path

from google.protobuf.json_format import MessageToDict

from archiver.feed import Feed
from archiver.parser import ParseFailure
from archiver.payloads import digest_timestamps, iter_payloads
from archiver.source import Source


def build_alert_snapshot(feed: Feed, day: date, source: Source) -> dict:
    """Merge a day's raw .bin polls into one last-write-wins alert snapshot.

    Reads the (feed, day) raw poll files in timestamp order, decodes each, and
    folds every alert entity into a dict keyed by alert id. The newest poll's
    body wins per id, while first_seen / last_seen / poll_count accumulate
    across all polls. Polls that fail to parse are skipped, not fatal.

    Raw .bin files aren't one-poll-per-file — legacy files are, but
    BatchingWriter/hourly-merged files are framed batches of many polls.
    archiver.payloads.iter_payloads unpacks whichever shape a file is; neither
    Source implementation guarantees iter_bins order, so the unpacked
    (payload, fetched_at) pairs are explicitly re-sorted by fetched_at before
    folding — order is load-bearing here for last-write-wins correctness.
    """
    digest_ts = digest_timestamps(source, feed.name, day)
    polls: list[tuple[bytes, int]] = []
    for name, blob in source.iter_bins(feed.name, day):
        polls.extend(iter_payloads(name, blob, digest_ts))
    polls.sort(key=lambda poll: poll[1])

    alerts: dict[str, dict] = {}
    last_header: dict | None = None

    for payload, fetched_at in polls:
        try:
            feed_message = feed.parser.parse(payload)
        except ParseFailure:
            continue

        for entity in feed_message.entity:
            if not entity.HasField("alert"):
                continue
            alert_id = entity.id
            alert_dict = MessageToDict(entity.alert, preserving_proto_field_name=True)
            existing = alerts.get(alert_id)
            if existing is None:
                alerts[alert_id] = {
                    "alert": alert_dict,
                    "first_seen": fetched_at,
                    "last_seen": fetched_at,
                    "poll_count": 1,
                }
            else:
                existing["alert"] = alert_dict
                existing["last_seen"] = fetched_at
                existing["poll_count"] += 1

        last_header = MessageToDict(
            feed_message.header, preserving_proto_field_name=True
        )

    return {
        "feed": feed.name,
        "service_date": day.isoformat(),
        "snapshot_timestamp": int(datetime.now(timezone.utc).timestamp()),
        "feed_header": last_header,
        "alerts": alerts,
    }


def snapshot_path(base_dir: Path, feed_name: str, day: date) -> Path:
    """The data.json.gz path for one (feed, day) snapshot (see module docstring)."""
    return (
        base_dir
        / "snapshots"
        / "alerts"
        / f"feed={feed_name}"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
        / "data.json.gz"
    )


def write_alert_snapshot(snapshot: dict, base_dir: Path) -> Path:
    """Gzip-write a snapshot to its canonical path; returns that path.

    Writes to a .tmp sibling and renames on success so a reader never sees a
    half-written file.
    """
    feed_name = snapshot["feed"]
    day = date.fromisoformat(snapshot["service_date"])
    out_path = snapshot_path(base_dir, feed_name, day)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / (out_path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    tmp.rename(out_path)
    return out_path


def load_alert_snapshot(path: Path) -> dict:
    """Load a snapshot dict, transparently handling gzipped or plain JSON."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)
