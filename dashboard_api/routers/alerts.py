# dashboard_api/routers/alerts.py

from datetime import date

from fastapi import APIRouter, Depends

from dashboard_api import dependencies
from dashboard_api.schemas.alert import AlertRow
from dashboard_api.services import alerts
from dashboard_api.services.agencies import Agency

router = APIRouter(prefix="/agencies/{agency}", tags=["alerts"])


@router.get("/alerts/{day}", response_model=list[AlertRow])
def get_alerts(
    day: date,
    agency: Agency = Depends(dependencies.get_agency),
) -> list[dict]:
    """Deduplicated alerts for one agency on one day."""
    return alerts.get_alerts(agency.feed_names, day)
