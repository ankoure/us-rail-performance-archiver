from pydantic import BaseModel


class StopRow(BaseModel):
    """Pydantic model for GTFS_STOPS_SCHEMA (pipeline/gtfs.py), current version only."""

    stop_id: str
    stop_code: str | None
    stop_name: str | None
    stop_lat: float | None
    stop_lon: float | None
