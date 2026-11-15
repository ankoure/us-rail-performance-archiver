from fastapi import APIRouter, Depends, HTTPException, Query

from api import dependencies
from api.schemas.agency import AgencyMetricsSummary, AgencySummary
from api.services import agencies, agency_summary

router = APIRouter(tags=["agencies"])


@router.get("/agencies", response_model=list[AgencySummary])
def get_agencies() -> list[AgencySummary]:
    rows = agencies.list_agencies()
    return [
        AgencySummary(
            agency_id=a.agency_id,
            name=a.name,
            timezone=a.timezone,
            continent=a.continent,
            region=a.region,
            types=a.types,
            type=a.types[0] if a.types else None,
            accent_color=a.accent_color,
            tagline=a.tagline,
            logo=a.logo,
        )
        for a in rows
    ]


@router.get("/agencies_summary", response_model=list[AgencyMetricsSummary])
def get_agencies_summary(
    dates: dependencies.DateRange = Depends(dependencies.date_range()),
    agency: list[str] = Query(..., min_length=1),
) -> list[dict]:
    """Headline metrics (OTP %, avg speed, delay minutes) for several agencies
    over the same date range, for side-by-side comparison."""
    resolved = []
    for agency_id in agency:
        try:
            resolved.append(agencies.get_agency(agency_id=agency_id))
        except agencies.AgencyNotFound:
            valid_ids = sorted(a.agency_id for a in agencies.list_agencies())
            raise HTTPException(
                status_code=404,
                detail=f"Unknown agency {agency_id!r}. Valid agencies: {valid_ids}",
            ) from None

    try:
        return [
            agency_summary.get_agency_metrics_summary(
                a, dates.start_date, dates.end_date
            )
            for a in resolved
        ]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
