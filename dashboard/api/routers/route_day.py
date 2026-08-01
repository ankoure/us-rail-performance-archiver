# dashboard/api/routers/route_day.py

from datetime import date

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.route_day import RouteDayRow
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["route_day"])


@router.get("/route_day", response_model=list[RouteDayRow])
def get_route_day(
    start_date: date,
    end_date: date,
    route_id: list[str] | None = Query(None),
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    """Per-route daily headway/dwell aggregates for one agency over a date range."""
    filters: dict[str, list[str]] = {}
    if route_id is not None:
        filters["route_id"] = route_id

    table = data.read_kind(
        "route_day", agency.feed_names, start_date, end_date, filters=filters
    )
    return table.to_pylist()
