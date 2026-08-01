# dashboard_api/routers/line_delays.py

from datetime import date

from fastapi import APIRouter, Depends

from dashboard_api import dependencies
from dashboard_api.schemas.line_delays import LineDelaysSummary
from dashboard_api.services import alerts
from dashboard_api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["line_delays"])


@router.get("/line_delays/{day}", response_model=LineDelaysSummary)
def get_line_delays(
    day: date,
    agency: Agency = Depends(dependencies.get_agency),
) -> dict:
    """Delay classification summary for one agency on one day."""
    return alerts.get_line_delays(agency.feed_names, day)
