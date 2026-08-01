from pydantic import BaseModel


class Translation(BaseModel):
    text: str
    language: str


class TranslatedString(BaseModel):
    translation: list[Translation] = []


class TimeRange(BaseModel):
    start: str | None = None
    end: str | None = None


class TripDescriptor(BaseModel):
    trip_id: str
    route_id: str
    direction_id: int


class InformedEntity(BaseModel):
    agency_id: str | None = None
    route_id: str | None = None
    route_type: int | None = None
    stop_id: str | None = None
    direction_id: int | None = None
    trip: TripDescriptor | None = None


class Alert(BaseModel):
    active_period: list[TimeRange] = []
    informed_entity: list[InformedEntity]
    cause: str | None = None
    effect: str | None = None
    url: TranslatedString | None = None
    header_text: TranslatedString | None = None
    description_text: TranslatedString | None = None
    severity_level: str | None = None


class AlertRow(BaseModel):
    alert_id: str
    first_seen: int
    last_seen: int
    poll_count: int
    alert: Alert
