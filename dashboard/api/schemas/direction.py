from pydantic import BaseModel


class DirectionRow(BaseModel):
    """Pydantic model for GTFS_DIRECTIONS_SCHEMA (pipeline/gtfs.py), current
    version only. An MBTA GTFS extension -- most non-MBTA feeds omit
    directions.txt, so this comes back empty for those agencies."""

    route_id: str
    direction_id: int
    direction: str | None
    direction_destination: str | None
