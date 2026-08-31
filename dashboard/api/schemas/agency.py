from pydantic import BaseModel


class AgencySummary(BaseModel):
    agency_id: str
    name: str
    timezone: str
    continent: str | None = None
    region: str | None = None
    #: Every mode the agency operates; an agency appears under each of them
    #: in the browse UI. See scripts/gen_agency_metadata.py.
    types: list[str] = []
    #: Deprecated: the first tag in `types`. Kept only so a dashboard build
    #: deployed before the tag migration still classifies agencies.
    type: str | None = None
    accent_color: str | None = None
    tagline: str | None = None
    logo: str | None = None


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
