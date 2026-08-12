from pydantic import BaseModel


class AgencySummary(BaseModel):
    agency_id: str
    name: str
    timezone: str


class AgencyMetricsSummary(BaseModel):
    """One agency's headline metrics over a date range — for cross-agency comparison."""

    agency_id: str
    name: str
    on_time_pct: float | None
    matched_count: int
    avg_speed_mph: float | None
    total_delay_minutes: int
    alert_count: int
    delay_alert_count: int
