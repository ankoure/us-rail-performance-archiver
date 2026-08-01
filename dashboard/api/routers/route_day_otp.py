# dashboard/api/routers/route_day_otp.py
from datetime import date

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.otp import RouteDayOtp
from api.services.agencies import Agency
from api.services import data

router = APIRouter(prefix="/agencies/{agency}", tags=["route_day_otp"])


@router.get("/route_day_otp", response_model=list[RouteDayOtp])
def get_route_day_otp(
    start_date: date,
    end_date: date,
    route_id: list[str] | None = Query(None),
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    filters: dict[str, list[str]] = {}
    if route_id is not None:
        filters["route_id"] = route_id

    table = data.read_kind(
        "route_day_otp", agency.feed_names, start_date, end_date, filters=filters
    )
    return table.to_pylist()
