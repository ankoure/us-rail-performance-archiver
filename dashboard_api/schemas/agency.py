from pydantic import BaseModel


class AgencySummary(BaseModel):
    agency_id: str
    name: str
    timezone: str
