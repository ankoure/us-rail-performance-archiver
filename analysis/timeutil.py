"""Local-time helpers shared across the analysis layer.

Deliberately dependency-light (stdlib only): the schedule-free gold path
(`metrics.py` → `gold.py`) and `archiver/shipper.py` import from here, so it
must not drag in pandas. The GTFS join (`static_gtfs.py`, which is
pandas-backed) lives behind the lazy `analysis/__init__.py` and the OTP-only
import path in `gold.py`.

Two notions of "date" live here, and they are not interchangeable:

  - `service_date` — the plain local *calendar* date of a timestamp. Used by
    the gobble-compatible CSV export (`event_export.py`), whose format is fixed.

  - `service_day` — the transit *operating* day. It runs from
    `SERVICE_DAY_START` local on day D to `SERVICE_DAY_START` local on D+1, so
    an owl visit at 01:30 belongs to the previous evening's service, the same
    day `adherence.py`'s schedule match files it under. Every gold mart keys
    and partitions on this, so one service day is exactly one partition and one
    row per (route, stop, service_date) — see issue #118.

Silver is partitioned by *UTC* day (rollup's landing layout), so building one
service day means reading every UTC partition its window overlaps; the
`service_day_*`/`utc_days_*` helpers below do that arithmetic.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

# Local wall-clock time at which one service day ends and the next begins.
# 03:00 is the common transit convention: late enough to keep owl trips
# (GTFS times 24:00–27:00) on the evening they belong to, early enough that
# morning service starts a new day. It also always exists: DST gaps in every
# zone we serve are 00:00–01:00 or 02:00–03:00.
SERVICE_DAY_START = dt.timedelta(hours=3)

# Extra silver read on each side of a service day's window when building it.
# Lets a dwell or segment that straddles the 03:00 boundary be seen whole
# rather than truncated; anything assigned to a neighbouring service day is
# filtered back out.
SERVICE_DAY_MARGIN = dt.timedelta(hours=1)

_UTC = dt.timezone.utc


def service_date(unix_ts: int, local_tz: ZoneInfo) -> dt.date:
    """The local calendar date a unix timestamp falls on, in `local_tz`."""
    return dt.datetime.fromtimestamp(unix_ts, tz=local_tz).date()


def service_day(unix_ts: int, local_tz: ZoneInfo) -> dt.date:
    """The transit operating day a unix timestamp belongs to, in `local_tz`.

    A timestamp before `SERVICE_DAY_START` local belongs to the previous day.
    """
    local = dt.datetime.fromtimestamp(unix_ts, tz=local_tz)
    return (local - SERVICE_DAY_START).date()


def service_day_bounds(day: dt.date, local_tz: ZoneInfo) -> tuple[int, int]:
    """[start, end) unix seconds of service day `day` in `local_tz`.

    `service_day(ts) == day` exactly when `start <= ts < end`.
    """

    def start_of(d: dt.date) -> int:
        midnight = dt.datetime.combine(d, dt.time(), tzinfo=local_tz)
        # Wall-clock add, then let the zone resolve the offset at 03:00 itself
        # (not midnight's), so DST-change days still start at local 03:00.
        wall = (midnight + SERVICE_DAY_START).replace(tzinfo=None)
        return int(wall.replace(tzinfo=local_tz).timestamp())

    return start_of(day), start_of(day + dt.timedelta(days=1))


def utc_days_for_service_day(day: dt.date, local_tz: ZoneInfo) -> list[dt.date]:
    """Every UTC day whose silver partition overlaps service day `day`,
    including `SERVICE_DAY_MARGIN` on each side, in ascending order."""
    start, end = service_day_bounds(day, local_tz)
    margin = int(SERVICE_DAY_MARGIN.total_seconds())
    first = dt.datetime.fromtimestamp(start - margin, tz=_UTC).date()
    # end is exclusive, so the last second we need is end + margin - 1.
    last = dt.datetime.fromtimestamp(end + margin - 1, tz=_UTC).date()
    out = [first]
    while out[-1] < last:
        out.append(out[-1] + dt.timedelta(days=1))
    return out


def service_day_for_utc_day(utc_day: dt.date, local_tz: ZoneInfo) -> dt.date:
    """The service day that UTC day `utc_day`'s silver partition completes.

    The nightly batch rolls up one UTC day at a time, then runs gold. The
    service day gold can build at that point is the latest one whose window
    (plus margin) ends by the end of `utc_day`. Partitions after `utc_day`
    don't exist yet. One UTC day maps to exactly one service day, and
    consecutive UTC days map to consecutive service days, so the nightly run
    builds each service day once with no gaps:

        America/New_York  utc_day 2026-06-11  ->  2026-06-10
        Europe/Paris      utc_day 2026-06-11  ->  2026-06-10
        Australia/Sydney  utc_day 2026-06-11  ->  2026-06-11
    """
    cutoff = int(
        dt.datetime.combine(
            utc_day + dt.timedelta(days=1), dt.time(), tzinfo=_UTC
        ).timestamp()
    )
    margin = int(SERVICE_DAY_MARGIN.total_seconds())
    # Local date at the cutoff is an upper bound (its own window can't have
    # ended yet); step back until the window plus margin fits. At most 2 steps.
    day = dt.datetime.fromtimestamp(cutoff, tz=local_tz).date()
    while service_day_bounds(day, local_tz)[1] + margin > cutoff:
        day -= dt.timedelta(days=1)
    return day


def fmt_local(unix_ts: int, local_tz: ZoneInfo) -> str:
    """ISO-ish local-time string matching gobble: '2026-03-23 06:30:39-04:00'."""
    local = dt.datetime.fromtimestamp(unix_ts, tz=local_tz)
    return local.isoformat(sep=" ", timespec="seconds")
