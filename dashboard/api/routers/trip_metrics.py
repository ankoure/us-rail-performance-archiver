# dashboard/api/routers/trip_metrics.py

from fastapi import APIRouter, Depends

from api import dependencies
from api.schemas.trip_metrics import TripMetrics
from api.services import trip_metrics
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["trip_metrics"])


@router.get("/trip_metrics", response_model=TripMetrics)
def get_trip_metrics(
    route_id: str,
    from_stop_id: str,
    to_stop_id: str,
    dates: dependencies.DateRange = Depends(
        dependencies.date_range(dependencies.MAX_EVENTS_RANGE_DAYS)
    ),
    direction_id: int | None = None,
    aggregate: bool = False,
    agency: Agency = Depends(dependencies.get_agency),
) -> dict:
    """Travel times and headways between one ordered pair of stops on a route.

    `aggregate=false` returns one row per vehicle run (the single-day view);
    `aggregate=true` returns per-service-date percentiles (the multi-day view).
    Only the requested shape is populated, so a long range doesn't ship every
    individual run just to draw a trend line.
    """
    result = trip_metrics.compute(
        agency.feed_names,
        route_id=route_id,
        from_stop_id=from_stop_id,
        to_stop_id=to_stop_id,
        start_date=dates.start_date,
        end_date=dates.end_date,
        direction_id=direction_id,
    )
    if aggregate:
        result["runs"] = []
    else:
        result["days"] = []
    return result
