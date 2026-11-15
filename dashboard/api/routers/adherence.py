# dashboard/api/routers/adherence.py

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.adherence import Adherence
from api.services.agencies import Agency
from api.services import data

router = APIRouter(prefix="/agencies/{agency}", tags=["adherence"])


@router.get("/adherence", response_model=list[Adherence])
def get_adherence(
    dates: dependencies.DateRange = Depends(dependencies.date_range()),
    stop_id: list[str] | None = Query(None),
    route_id: list[str] | None = Query(None),
    trip_id: list[str] | None = Query(None),
    limit: int = Query(1000, le=10000),
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    filters: dict[str, list[str]] = {}
    if stop_id is not None:
        filters["stop_id"] = stop_id
    if route_id is not None:
        filters["route_id"] = route_id
    if trip_id is not None:
        filters["trip_id"] = trip_id

    table = data.read_kind(
        "adherence",
        agency.feed_names,
        dates.start_date,
        dates.end_date,
        filters=filters,
        limit=limit,
    )
    return table.to_pylist()
