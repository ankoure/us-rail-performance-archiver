from pydantic import BaseModel


class RouteDayRow(BaseModel):
    feed: str
    route_id: str
    direction_id: int | None
    service_date: str
    visit_count: int
    trip_count: int
    distinct_vehicle_count: int
    distinct_stop_count: int
    headway_p50_s: int | None
    dwell_p50_s: int | None
    dwell_p90_s: int | None
    first_service_unix: int
    last_service_unix: int
    service_span_s: int
