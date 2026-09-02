# dashboard/api/routers/directions.py

from fastapi import APIRouter, Depends, HTTPException

from api import dependencies
from api.schemas.direction import DirectionRow
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["directions"])


@router.get("/directions", response_model=list[DirectionRow])
def get_directions(agency: Agency = Depends(dependencies.get_agency)) -> list[dict]:
    """Current (route_id, direction_id) -> direction label/destination for one
    agency, resolved from the latest static-GTFS snapshot each of its feeds has
    landed. directions.txt is an MBTA GTFS
    extension -- most other agencies' feeds omit it, so this comes back empty
    for those, not an error."""
    if not agency.feed_names:
        raise HTTPException(
            status_code=404, detail=f"Agency {agency.agency_id!r} has no feeds"
        )
    table = data.read_latest_version_mart("gtfs_directions", agency.feed_names)
    return table.to_pylist()
