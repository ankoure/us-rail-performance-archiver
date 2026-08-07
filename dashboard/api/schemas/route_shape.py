from pydantic import BaseModel


class RouteShapePoint(BaseModel):
    """Pydantic model for ROUTE_SHAPES_SCHEMA (pipeline/gtfs.py), current version only."""

    route_id: str
    direction_id: int
    shape_id: str
    point_sequence: int
    lat: float
    lon: float
    dist_m: float


class RouteShapeStop(BaseModel):
    """Pydantic model for ROUTE_SHAPE_STOPS_SCHEMA (pipeline/gtfs.py), current version only."""

    route_id: str
    direction_id: int
    shape_id: str
    stop_id: str
    dist_m: float


class RouteShapeResponse(BaseModel):
    """One route's canonical polyline(s) plus each stop's offset along them,
    covering every direction_id the route has a canonical shape for."""

    points: list[RouteShapePoint]
    stops: list[RouteShapeStop]
