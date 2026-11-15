# dashboard/api/routers/stop_day_otp.py

from fastapi import APIRouter, Depends, Query

from api import dependencies
from api.schemas.otp import StopDayOtp
from api.services.agencies import Agency
from api.services import data

router = APIRouter(prefix="/agencies/{agency}", tags=["stop_day_otp"])


@router.get("/stop_day_otp", response_model=list[StopDayOtp])
def get_stop_day_otp(
    dates: dependencies.DateRange = Depends(dependencies.date_range()),
    stop_id: list[str] | None = Query(None),
    route_id: list[str] | None = Query(None),
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    filters: dict[str, list[str]] = {}
    if stop_id is not None:
        filters["stop_id"] = stop_id
    if route_id is not None:
        filters["route_id"] = route_id

    table = data.read_kind(
        "stop_day_otp",
        agency.feed_names,
        dates.start_date,
        dates.end_date,
        filters=filters,
    )
    return table.to_pylist()
