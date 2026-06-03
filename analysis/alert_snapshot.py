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


def build_alert_snapshot(feed: Feed, day: date, landing_dir: Path) -> dict:
    raw_dir = (
        landing_dir
        / feed.name
        / "raw"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
    )
    bin_files = sorted(raw_dir.glob("*.bin"), key=lambda p: float(p.stem))

    alerts: dict[str, dict] = {}
    last_header: dict | None = None

    for bin_file in bin_files:
        fetched_at = int(float(bin_file.stem))
        try:
            feed_message = feed.parser.parse(bin_file.read_bytes())
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
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)
