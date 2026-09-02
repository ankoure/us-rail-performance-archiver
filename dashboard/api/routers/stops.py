# dashboard/api/routers/stops.py

from fastapi import APIRouter, Depends, HTTPException

from api import dependencies
from api.schemas.stop import StopRow
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["stops"])


@router.get("/stops", response_model=list[StopRow])
def get_stops(agency: Agency = Depends(dependencies.get_agency)) -> list[dict]:
    """Current stop_id -> stop_name/coordinates for one agency, resolved from
    the latest static-GTFS snapshot each of its feeds has landed. All of an agency's feeds share the same underlying schedule, but
    not all of them land a snapshot of it, so reading only the first would come
    back empty for agencies whose first feed is alerts-only."""
    if not agency.feed_names:
        raise HTTPException(
            status_code=404, detail=f"Agency {agency.agency_id!r} has no feeds"
        )
    table = data.read_latest_version_mart("gtfs_stops", agency.feed_names)
    return table.to_pylist()
