# dashboard/api/dependencies.py

from dataclasses import dataclass
from datetime import date
from typing import Callable

from fastapi import HTTPException

from api.services import agencies

# Widest [start_date, end_date] span the parquet-backed endpoints accept. Every
# extra month is another set of partitions scanned into one Arrow table and
# then to_pylist()'d, inside a container capped at mem_limit: 900m
# (compose.prod.yml), so an unbounded range is an easy OOM-kill. A year covers
# any range the dashboard UI realistically asks for.
MAX_RANGE_DAYS = 366

# trip_metrics scans the raw per-arrival events mart rather than a daily
# rollup, so it's roughly an order of magnitude heavier per day. Matches
# services/alerts._MAX_RANGE_DAYS.
MAX_EVENTS_RANGE_DAYS = 62


def get_agency(agency: str) -> agencies.Agency:
    """FastAPI Depends() callable — binds from a path param literally named `agency`.

    Raises HTTPException(404) with the list of valid agency ids if not found.
    """
    try:
        return agencies.get_agency(agency_id=agency)
    except agencies.AgencyNotFound:
        valid_ids = sorted(a.agency_id for a in agencies.list_agencies())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown agency {agency!r}. Valid agencies: {valid_ids}",
        ) from None


@dataclass(frozen=True)
class DateRange:
    start_date: date
    end_date: date


def date_range(max_days: int = MAX_RANGE_DAYS) -> Callable[[date, date], DateRange]:
    """Build a FastAPI Depends() callable binding the `start_date`/`end_date`
    query params, rejecting reversed ranges and spans wider than max_days
    (counted as end_date - start_date, the same convention as
    services/alerts._MAX_RANGE_DAYS) with a 400."""

    def _dependency(start_date: date, end_date: date) -> DateRange:
        if start_date > end_date:
            raise HTTPException(
                status_code=400,
                detail=f"start_date {start_date} is after end_date {end_date}",
            )
        if (end_date - start_date).days > max_days:
            raise HTTPException(
                status_code=400,
                detail=f"Range too wide; max {max_days} days",
            )
        return DateRange(start_date=start_date, end_date=end_date)

    return _dependency
