# dashboard_api/services/alerts.py

import json
from datetime import date

from dashboard_api.services.data import _hot_path, _s3_filesystem


def _snapshot_path(feed_name: str, day: date) -> str:
    return _hot_path(
        f"snapshots/alerts/feed={feed_name}/year={day.year}/month={day.month}/day={day.day}/data.json.gz"
    )


def get_alerts(feed_names: list[str], day: date) -> list[dict]:
    """Merge alert snapshots across an agency's feeds for one day.

    dashboard_api has no decoder-capability info (that lives in archiver/,
    which this package doesn't depend on) to know up front which of an
    agency's feeds actually produce alerts — so this tries each feed name and
    silently skips the ones with no snapshot for this day, rather than trying
    to predict which feed is "the alerts feed" from its name.
    """
    fs = _s3_filesystem()
    rows: list[dict] = []
    for feed_name in feed_names:
        try:
            with fs.open_input_stream(_snapshot_path(feed_name, day)) as f:
                raw = f.readall()
        except FileNotFoundError:
            continue
        snapshot = json.loads(raw)
        for alert_id, entry in snapshot["alerts"].items():
            rows.append({"alert_id": alert_id, **entry})
    return rows
