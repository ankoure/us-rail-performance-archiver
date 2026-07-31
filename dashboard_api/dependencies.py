# dashboard_api/dependencies.py

from fastapi import HTTPException

from dashboard_api.services import agencies


def get_agency(agency: str) -> agencies.Agency:
    """FastAPI Depends() callable — binds from a path param literally named `agency`.

    Raises HTTPException(404) with the list of valid agency ids if not found.
    """
    try:
        return agencies.get_agency(agency_id=agency)
    except agencies.AgencyNotFound:
        valid_ids = sorted(a.agency_id for a in agencies.list_agencies())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown agency {agency!r}. Valid agencies: {valid_ids}",
        ) from None