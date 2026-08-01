# dashboard/api/services/alerts.py

import json
from datetime import date

from api.services.data import _hot_path, _s3_filesystem
from analysis.alert_classifier import DELAY_BY_TYPE, summarize_snapshot


def _snapshot_path(feed_name: str, day: date) -> str:
    return _hot_path(
        f"snapshots/alerts/feed={feed_name}/year={day.year}/month={day.month}/day={day.day}/data.json.gz"
    )


def _fetch_snapshot(feed_name: str, day: date) -> dict | None:
    """Fetch and parse one feed's raw snapshot dict, or None if absent for this day."""
    fs = _s3_filesystem()
    try:
        with fs.open_input_stream(_snapshot_path(feed_name, day)) as f:
            raw = f.readall()
    except FileNotFoundError:
        return None
    return json.loads(raw)


def _merge_summaries(summaries: list[dict]) -> dict:
    """Combine per-feed summarize_snapshot() results into one agency-level total."""
    if not summaries:
        return {
            "feeds": [],
            "service_date": None,
            "alert_count": 0,
            "delay_alert_count": 0,
            "total_delay_minutes": 0,
            "delay_by_type": dict(DELAY_BY_TYPE),
            "count_by_type": dict(DELAY_BY_TYPE),
        }

    return {
        "feeds": [s["feed"] for s in summaries],
        "service_date": summaries[0]["service_date"],
        "alert_count": sum(s["alert_count"] for s in summaries),
        "delay_alert_count": sum(s["delay_alert_count"] for s in summaries),
        "total_delay_minutes": sum(s["total_delay_minutes"] for s in summaries),
        "delay_by_type": {
            label: sum(s["delay_by_type"][label] for s in summaries)
            for label in DELAY_BY_TYPE
        },
        "count_by_type": {
            label: sum(s["count_by_type"][label] for s in summaries)
            for label in DELAY_BY_TYPE
        },
    }


def get_line_delays(feed_names: list[str], day: date) -> dict:
    """Delay classification summary for an agency's alerts on one day, merged
    across however many of its feeds actually produced alerts that day."""
    summaries: list[dict] = []
    for feed_name in feed_names:
        snapshot = _fetch_snapshot(feed_name, day)
        if snapshot is None:
            continue
        summaries.append(summarize_snapshot(snapshot))
    return _merge_summaries(summaries)


def get_alerts(feed_names: list[str], day: date) -> list[dict]:
    """Merge alert snapshots across an agency's feeds for one day.

    dashboard/api has no decoder-capability info (that lives in archiver/,
    which this package doesn't depend on) to know up front which of an
    agency's feeds actually produce alerts — so this tries each feed name and
    silently skips the ones with no snapshot for this day, rather than trying
    to predict which feed is "the alerts feed" from its name.
    """
    rows: list[dict] = []
    for feed_name in feed_names:
        snapshot = _fetch_snapshot(feed_name, day)
        if snapshot is None:
            continue
        for alert_id, entry in snapshot["alerts"].items():
            rows.append({"alert_id": alert_id, **entry})
    return rows
