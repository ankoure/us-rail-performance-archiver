# dashboard/api/routers/segment_day.py

from datetime import date

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.segment_day import SegmentDayRow
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["segment_day"])


@router.get("/segment_day", response_model=list[SegmentDayRow])
def get_segment_day(
    start_date: date,
    end_date: date,
    route_id: list[str] | None = Query(None),
    from_stop_id: list[str] | None = Query(None),
    to_stop_id: list[str] | None = Query(None),
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    """Per-segment daily inter-stop speed/travel-time aggregates for one agency over a date range."""
    filters: dict[str, list[str]] = {}
    if route_id is not None:
        filters["route_id"] = route_id
    if from_stop_id is not None:
        filters["from_stop_id"] = from_stop_id
    if to_stop_id is not None:
        filters["to_stop_id"] = to_stop_id

    table = data.read_kind(
        "segment_day", agency.feed_names, start_date, end_date, filters=filters
    )
    return table.to_pylist()
