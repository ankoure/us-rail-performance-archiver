# dashboard_api/routers/adherence.py
from datetime import date

from fastapi import APIRouter, Depends, Query

from dashboard_api import dependencies
from dashboard_api.schemas.adherence import Adherence
from dashboard_api.services.agencies import Agency
from dashboard_api.services import data

router = APIRouter(prefix="/agencies/{agency}", tags=["adherence"])


@router.get("/adherence", response_model=list[Adherence])
def get_adherence(
    start_date: date,
    end_date: date,
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
        start_date,
        end_date,
        filters=filters,
        limit=limit,
    )
    return table.to_pylist()
