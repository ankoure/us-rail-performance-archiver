from fastapi import APIRouter

from api.schemas.agency import AgencySummary
from api.services import agencies

router = APIRouter(tags=["agencies"])


@router.get("/agencies", response_model=list[AgencySummary])
def get_agencies(rail_only: bool = False) -> list[AgencySummary]:
    rows = agencies.list_rail_agencies() if rail_only else agencies.list_agencies()
    return [
        AgencySummary(agency_id=a.agency_id, name=a.name, timezone=a.timezone)
        for a in rows
    ]
