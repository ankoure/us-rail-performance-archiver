from fastapi import APIRouter

from api.schemas.agency import AgencySummary
from api.services import agencies

router = APIRouter(tags=["agencies"])


@router.get("/agencies", response_model=list[AgencySummary])
def get_agencies() -> list[AgencySummary]:
    return [
        AgencySummary(agency_id=a.agency_id, name=a.name, timezone=a.timezone)
        for a in agencies.list_agencies()
    ]
