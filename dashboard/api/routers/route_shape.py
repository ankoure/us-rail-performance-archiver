# dashboard/api/routers/route_shape.py

import pyarrow.compute as pc
from fastapi import APIRouter, Depends, HTTPException

from api import dependencies
from api.schemas.route_shape import RouteShapeResponse
from api.services import data
from api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["route_shape"])


def _filter_to_route(table, route_id: str):
    if "route_id" not in table.column_names:
        return table
    return table.filter(pc.field("route_id") == route_id)


@router.get("/route_shape", response_model=RouteShapeResponse)
def get_route_shape(
    route_id: str,
    agency: Agency = Depends(dependencies.get_agency),
) -> RouteShapeResponse:
    """Canonical polyline + stop offsets for one route (every direction it
    has a canonical shape for), resolved from the latest static-GTFS
    snapshot — for plotting segment speeds on a map. See
    analysis.static_gtfs.StaticGtfs.route_direction_shapes for how the
    canonical shape per direction is chosen."""
    if not agency.feed_names:
        raise HTTPException(
            status_code=404, detail=f"Agency {agency.agency_id!r} has no feeds"
        )
    feeds = agency.feed_names
    points = _filter_to_route(
        data.read_latest_version_mart("route_shapes", feeds), route_id
    )
    stops = _filter_to_route(
        data.read_latest_version_mart("route_shape_stops", feeds), route_id
    )
    return RouteShapeResponse(points=points.to_pylist(), stops=stops.to_pylist())
