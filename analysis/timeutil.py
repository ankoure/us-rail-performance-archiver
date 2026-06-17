"""Local-time helpers shared across the analysis layer.

Deliberately dependency-light (stdlib only): the schedule-free gold path
(`metrics.py` → `gold.py`) imports `service_date` from here, so it must not drag
in pandas. The GTFS join (`static_gtfs.py`, which is pandas-backed) lives behind
the lazy `analysis/__init__.py` and the OTP-only import path in `gold.py`.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def service_date(unix_ts: int, local_tz: ZoneInfo) -> dt.date:
    """The local calendar date a unix timestamp falls on, in `local_tz`."""
    return dt.datetime.fromtimestamp(unix_ts, tz=local_tz).date()


def fmt_local(unix_ts: int, local_tz: ZoneInfo) -> str:
    """ISO-ish local-time string matching gobble: '2026-03-23 06:30:39-04:00'."""
    local = dt.datetime.fromtimestamp(unix_ts, tz=local_tz)
    return local.isoformat(sep=" ", timespec="seconds")
