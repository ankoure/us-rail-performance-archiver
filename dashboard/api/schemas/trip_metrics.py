from __future__ import annotations

from pydantic import BaseModel


class TripRun(BaseModel):
    """One vehicle's run between the chosen stop pair, from the events mart."""

    service_date: str
    trip_id: str
    vehicle_id: str | None = None
    direction_id: int | None = None
    departure_unix: int
    arrival_unix: int
    travel_time_s: int
    # Gap to the previous arrival at from_stop on the same service date and
    # direction. Null for the first vehicle of the day, which has no predecessor.
    headway_s: int | None = None


class TripDay(BaseModel):
    """One service date's distribution, for the multi-day view."""

    service_date: str
    trip_count: int
    travel_time_p10_s: int | None = None
    travel_time_p50_s: int | None = None
    travel_time_p90_s: int | None = None
    headway_p50_s: int | None = None
    headway_p90_s: int | None = None


class TripMetrics(BaseModel):
    """Travel times and headways between one ordered pair of stops.

    `runs` is populated for the single-day view and `days` for the aggregate
    view; the request picks which via `aggregate`, and the other stays empty
    so one response model serves both pages.
    """

    from_stop_id: str
    to_stop_id: str
    route_id: str
    direction_id: int | None = None
    runs: list[TripRun] = []
    days: list[TripDay] = []
