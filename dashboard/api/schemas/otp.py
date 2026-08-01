from __future__ import annotations

from pydantic import BaseModel


class OtpAggFields(BaseModel):
    """Shared aggregate fields (_AGG_FIELDS) common to stop- and route-grain OTP schemas."""

    matched_count: int
    on_time_count: int
    early_count: int
    late_count: int
    on_time_pct: float | None = None
    arr_delay_p50_s: int | None = None
    arr_delay_p90_s: int | None = None
    arr_delay_mean_s: float | None = None
    dep_delay_p50_s: int | None = None


class StopDayOtp(OtpAggFields):
    """Pydantic model for STOP_DAY_OTP_SCHEMA (analysis/adherence.py)."""

    feed: str
    route_id: str
    direction_id: int | None = None
    stop_id: str
    service_date: str


class RouteDayOtp(OtpAggFields):
    """Pydantic model for ROUTE_DAY_OTP_SCHEMA (analysis/adherence.py)."""

    feed: str
    route_id: str
    direction_id: int | None = None
    service_date: str
    distinct_stop_count: int
