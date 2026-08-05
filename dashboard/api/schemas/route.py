from pydantic import BaseModel


class RouteRow(BaseModel):
    """Pydantic model for ROUTES_SCHEMA (pipeline/gold.py), most recent day only."""

    route_id: str
    route_short_name: str | None
    route_long_name: str | None
    mode: str
