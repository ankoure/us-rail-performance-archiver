# dashboard/api/routers/segment_speed_map.py

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from api import dependencies
from api.schemas.segment_speed_map import SegmentSpeedMapResponse
from api.services.agencies import Agency
from api.services.segment_speed_map import build_segment_speed_map

router = APIRouter(prefix="/agencies/{agency}", tags=["segment_speed_map"])


@router.get("/segment_speed_map", response_model=SegmentSpeedMapResponse)
def get_segment_speed_map(
    route_id: str,
    start_date: date,
    end_date: date,
    agency: Agency = Depends(dependencies.get_agency),
) -> dict:
    """Ready-to-render segment speeds for one route/date-range: per-segment
    LineString geometry sliced from the route's static-GTFS shape, colored
    by a relative (this-route, this-range) speed bucket, plus the route's
    stops as Point features and the bucket legend. Replaces the old
    segment_day + route_shape + client-side aggregation/slicing pipeline for
    the map page -- see SegmentSpeedMap.tsx."""
    if not agency.feed_names:
        raise HTTPException(
            status_code=404, detail=f"Agency {agency.agency_id!r} has no feeds"
        )
    return build_segment_speed_map(agency, route_id, start_date, end_date)
