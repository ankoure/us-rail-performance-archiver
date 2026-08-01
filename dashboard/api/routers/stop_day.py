# dashboard/api/routers/stop_day.py

from datetime import date

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.stop_day import StopDayRow
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["stop_day"])


@router.get("/stop_day", response_model=list[StopDayRow])
def get_stop_day(
    start_date: date,
    end_date: date,
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
        "stop_day", agency.feed_names, start_date, end_date, filters=filters
    )
    return table.to_pylist()
