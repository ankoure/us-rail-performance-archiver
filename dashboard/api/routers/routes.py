# dashboard/api/routers/routes.py

from fastapi import APIRouter, Depends, HTTPException

from api import dependencies
from api.schemas.route import RouteRow
from api.services import data, route_metadata
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["routes"])


@router.get("/routes", response_model=list[RouteRow])
def get_routes(agency: Agency = Depends(dependencies.get_agency)) -> list[dict]:
    """Current routes manifest for one agency (route_id, names, mode), resolved
    from the most recent day any of its feeds has landed. All of an agency's
    feeds share the same underlying schedule, so the first is enough."""
    if not agency.feed_names:
        raise HTTPException(
            status_code=404, detail=f"Agency {agency.agency_id!r} has no feeds"
        )
    table = data.read_current_routes(agency.feed_names[0])
    rows = table.to_pylist()
    for row in rows:
        row["mode"] = route_metadata.resolve_mode(
            agency.agency_id, row["route_id"], row["mode"]
        )
    return rows
