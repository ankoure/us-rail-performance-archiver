import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from analysis.timeutil import (
    service_date,
    service_day,
    service_day_bounds,
    service_day_for_utc_day,
    utc_days_for_service_day,
)

NY = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
SYDNEY = ZoneInfo("Australia/Sydney")
UTC = ZoneInfo("UTC")

D = dt.date


def _local(y, m, d, hh, mm, tz) -> int:
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


class TestServiceDay:
    def test_before_three_am_is_previous_day(self):
        ts = _local(2026, 6, 11, 2, 59, NY)
        assert service_day(ts, NY) == D(2026, 6, 10)
        assert service_date(ts, NY) == D(2026, 6, 11)

    def test_three_am_starts_the_day(self):
        assert service_day(_local(2026, 6, 11, 3, 0, NY), NY) == D(2026, 6, 11)

    @pytest.mark.parametrize(
        "day, hours",
        [
            (D(2026, 3, 7), 23),  # spring forward on 03-08 at 02:00
            (D(2026, 10, 31), 25),  # fall back on 11-01 at 02:00
            (D(2026, 6, 10), 24),
        ],
    )
    def test_bounds_follow_dst(self, day, hours):
        start, end = service_day_bounds(day, NY)
        assert (end - start) == hours * 3600
        assert service_day(start, NY) == day
        assert service_day(end - 1, NY) == day
        assert service_day(end, NY) == day + dt.timedelta(days=1)


class TestUtcPartitions:
    @pytest.mark.parametrize(
        "tz, day, expected",
        [
            (NY, D(2026, 6, 10), [D(2026, 6, 10), D(2026, 6, 11)]),
            (SYDNEY, D(2026, 6, 11), [D(2026, 6, 10), D(2026, 6, 11)]),
            (UTC, D(2026, 6, 10), [D(2026, 6, 10), D(2026, 6, 11)]),
        ],
    )
    def test_overlapping_utc_days(self, tz, day, expected):
        assert utc_days_for_service_day(day, tz) == expected

    @pytest.mark.parametrize(
        "tz, expected",
        [(NY, D(2026, 6, 10)), (PARIS, D(2026, 6, 10)), (SYDNEY, D(2026, 6, 11))],
    )
    def test_service_day_completed_by_utc_day(self, tz, expected):
        assert service_day_for_utc_day(D(2026, 6, 11), tz) == expected

    @pytest.mark.parametrize(
        "tz", [NY, PARIS, SYDNEY, UTC, ZoneInfo("Pacific/Honolulu")]
    )
    def test_nightly_mapping_is_gapless_and_never_reads_ahead(self, tz):
        """Each night's UTC day builds the next service day, never needing a
        partition rollup hasn't produced yet, across both DST changes."""
        utc_day = D(2026, 1, 1)
        prev = service_day_for_utc_day(utc_day - dt.timedelta(days=1), tz)
        while utc_day < D(2027, 1, 1):
            sd = service_day_for_utc_day(utc_day, tz)
            assert sd == prev + dt.timedelta(days=1)
            assert max(utc_days_for_service_day(sd, tz)) <= utc_day
            prev = sd
            utc_day += dt.timedelta(days=1)
