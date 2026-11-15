# dashboard/api/routers/stop_day.py

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.stop_day import StopDayRow
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["stop_day"])


@router.get("/stop_day", response_model=list[StopDayRow])
def get_stop_day(
    dates: dependencies.DateRange = Depends(dependencies.date_range()),
    stop_id: list[str] | None = Query(None),
    route_id: list[str] | None = Query(None),
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    """Per-stop daily headway/dwell aggregates for one agency over a date range."""
    filters: dict[str, list[str]] = {}
    if stop_id is not None:
        filters["stop_id"] = stop_id
    if route_id is not None:
        filters["route_id"] = route_id

    table = data.read_kind(
        "stop_day", agency.feed_names, dates.start_date, dates.end_date, filters=filters
    )
    return table.to_pylist()
