from __future__ import annotations

from pydantic import BaseModel


class Adherence(BaseModel):
    """Pydantic model for ADHERENCE_SCHEMA (schemas/adherence.py)."""

    feed: str
    route_id: str
    direction_id: int | None = None
    stop_id: str
    stop_sequence: int | None = None
    trip_id: str
    vehicle_id: str | None = None
    route_mode: str | None = None
    service_date: str
    arrival_unix: int
    scheduled_arrival_unix: int | None = None
    arrival_delay_s: int | None = None
    departure_unix: int
    scheduled_departure_unix: int | None = None
    departure_delay_s: int | None = None
    status: str
    on_time: bool
