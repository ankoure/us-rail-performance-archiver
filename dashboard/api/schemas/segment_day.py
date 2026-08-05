from pydantic import BaseModel


class SegmentDayRow(BaseModel):
    """Pydantic model for SEGMENT_DAY_SCHEMA (analysis/segment_speed.py)."""

    feed: str
    route_id: str
    direction_id: int | None
    from_stop_id: str
    to_stop_id: str
    service_date: str
    sample_count: int
    speed_p50_mph: float | None
    speed_p90_mph: float | None
    speed_mean_mph: float | None
    transit_p50_s: int | None
    distance_m: float
