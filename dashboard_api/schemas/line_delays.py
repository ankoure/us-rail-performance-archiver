# dashboard_api/schemas/line_delays.py

from pydantic import BaseModel


class LineDelaysSummary(BaseModel):
    feeds: list[str]
    service_date: str | None
    alert_count: int
    delay_alert_count: int
    total_delay_minutes: int
    delay_by_type: dict[str, int]
    count_by_type: dict[str, int]
