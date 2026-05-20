from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Iterator
from zoneinfo import ZoneInfo
import pyarrow as pa
from google.transit import gtfs_realtime_pb2 as gtfs


@dataclass
class TableSpec:
    name: str
    dedup_keys: tuple[str, ...] = ()
    # Python dataclass field name -> parquet column name. Lets consumers (e.g. LAMP)
    # see dotted names like "vehicle.vehicle.id" that aren't valid Python identifiers.
    column_names: dict[str, str] = field(default_factory=dict)
    # Schema-only columns the decoder never populates, written as all-null.
    # Required when a downstream reader demands a column the upstream feed doesn't publish.
    extra_columns: tuple[pa.Field, ...] = ()


@dataclass
class Row:
    pass


@dataclass
class MartaPredictionRow(Row):
    feed_timestamp: int
    destination: str
    direction: str
    event_time: int
    is_realtime: bool
    line: str
    next_arr: int
    station: str
    train_id: str
    waiting_seconds: int
    waiting_time: str
    delay: int | None
    latitude: float | None
    longitude: float | None


@dataclass
class VehicleRow(Row):
    feed_timestamp: int | None = None
    vehicle_id: str | None = None
    vehicle_label: str | None = None
    trip_id: str | None = None
    route_id: str | None = None
    direction_id: int | None = None
    start_date: str | None = None
    start_time: str | None = None
    schedule_relationship: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bearing: float | None = None
    speed: float | None = None
    current_stop_sequence: int | None = None
    stop_id: str | None = None
    current_status: str | None = None
    occupancy_status: str | None = None
    occupancy_percentage: int | None = None
    vehicle_timestamp: int | None = None


@dataclass
class StopTimeUpdateRow(Row):
    feed_timestamp: int | None = None
    trip_update_timestamp: int | None = None
    trip_id: str | None = None
    route_id: str | None = None
    direction_id: int | None = None
    start_date: str | None = None
    start_time: str | None = None
    schedule_relationship: str | None = None  # TripDescriptor.ScheduleRelationship
    vehicle_id: str | None = None
    vehicle_label: str | None = None
    stop_sequence: int | None = None
    stop_id: str | None = None
    arrival_delay: int | None = None
    arrival_time: int | None = None
    arrival_uncertainty: int | None = None
    departure_delay: int | None = None
    departure_time: int | None = None
    departure_uncertainty: int | None = None
    stop_time_schedule_relationship: str | None = (
        None  # StopTimeUpdate.ScheduleRelationship
    )


@dataclass
class AlertRow(Row):
    feed_timestamp: int | None = None
    alert_id: str | None = None
    cause: str | None = None  # Alert.Cause
    effect: str | None = None  # Alert.Effect
    url: str | None = (
        None  # TranslatedString, one row per language or just header_text?
    )
    header_text: str | None = None
    description_text: str | None = None
    # informed_entity is repeated — typically one AlertRow per InformedEntity
    agency_id: str | None = None
    route_id: str | None = None
    route_type: int | None = None
    direction_id: int | None = None
    trip_id: str | None = None
    stop_id: str | None = None
    severity_level: str | None = None  # Alert.SeverityLevel


class DecodeFailure(Exception):
    """Raised when a decoder cannot parse the input bytes."""


class Decoder(ABC):
    produces: ClassVar[dict[type[Row], TableSpec]]

    # Registration happens as an import side effect — every module defining a
    # @Decoder.register(...) subclass must be imported before from_name() is called.
    _registry: ClassVar[dict[str, type["Decoder"]]] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(subclass: type["Decoder"]) -> type["Decoder"]:
            if name in cls._registry:
                raise ValueError(
                    f"decoder name {name!r} already registered to "
                    f"{cls._registry[name].__name__}"
                )
            cls._registry[name] = subclass
            return subclass

        return decorator

    @classmethod
    def from_name(cls, name: str) -> "Decoder":
        try:
            return cls._registry[name]()
        except KeyError:
            raise KeyError(
                f"no decoder registered for {name!r}; known: {sorted(cls._registry)}"
            ) from None

    @abstractmethod
    def decode(self, parsed: Any, *, fetched_at: int | None = None) -> Iterator[Row]:
        raise NotImplementedError


class GtfsRtDecoder(Decoder):
    """Any decoder for the GTFS-RT protobuf format. Subclasses provide entity-level hooks."""

    def decode(
        self, parsed: gtfs.FeedMessage, *, fetched_at: int | None = None
    ) -> Iterator[VehicleRow | StopTimeUpdateRow | AlertRow]:
        feed = parsed
        for entity in feed.entity:
            if entity.HasField("vehicle"):
                yield self._decode_vehicle(entity.vehicle, feed.header, fetched_at)
            elif entity.HasField("trip_update"):
                yield from self._decode_trip_update(
                    entity.trip_update, feed.header, fetched_at
                )
            elif entity.HasField("alert"):
                yield from self._decode_alert(
                    entity.alert, entity.id, feed.header, fetched_at
                )

    @abstractmethod
    def _decode_vehicle(self, vp, header, fetched_at: int | None = None) -> VehicleRow:
        raise NotImplementedError

    @abstractmethod
    def _decode_trip_update(
        self, tu, header, fetched_at: int | None = None
    ) -> Iterator[StopTimeUpdateRow]:
        raise NotImplementedError

    @abstractmethod
    def _decode_alert(
        self, sa, alert_id, header, fetched_at: int | None = None
    ) -> Iterator[AlertRow]:
        raise NotImplementedError

    @staticmethod
    def _opt(msg, field: str, transform=None):
        if not msg.HasField(field):
            return None
        value = getattr(msg, field)
        return transform(value) if transform else value


@Decoder.register("standard")
class StandardDecoder(GtfsRtDecoder):
    produces: ClassVar[dict[type[Row], TableSpec]] = {
        VehicleRow: TableSpec(
            "vehicles",
            dedup_keys=("vehicle_id", "vehicle_timestamp"),
            column_names={
                "vehicle_timestamp": "vehicle.timestamp",
                "current_status": "vehicle.current_status",
                "current_stop_sequence": "vehicle.current_stop_sequence",
                "stop_id": "vehicle.stop_id",
                "direction_id": "vehicle.trip.direction_id",
                "route_id": "vehicle.trip.route_id",
                "start_date": "vehicle.trip.start_date",
                "start_time": "vehicle.trip.start_time",
                "trip_id": "vehicle.trip.trip_id",
                "vehicle_id": "vehicle.vehicle.id",
                "vehicle_label": "vehicle.vehicle.label",
            },
            extra_columns=(
                pa.field("vehicle.trip.revenue", pa.bool_()),
                pa.field(
                    "vehicle.vehicle.consist",
                    pa.list_(pa.struct([pa.field("label", pa.string())])),
                ),
                pa.field(
                    "vehicle.multi_carriage_details",
                    pa.list_(pa.struct([pa.field("label", pa.string())])),
                ),
            ),
        ),
        StopTimeUpdateRow: TableSpec(
            "trip_updates",
            column_names={
                "trip_update_timestamp": "trip_update.timestamp",
                "trip_id": "trip_update.trip.trip_id",
                "route_id": "trip_update.trip.route_id",
                "direction_id": "trip_update.trip.direction_id",
                "start_date": "trip_update.trip.start_date",
                "start_time": "trip_update.trip.start_time",
                "vehicle_id": "trip_update.vehicle.id",
                "stop_id": "trip_update.stop_time_update.stop_id",
                "arrival_time": "trip_update.stop_time_update.arrival.time",
            },
        ),
        AlertRow: TableSpec("alerts"),
    }

    def _decode_vehicle(self, vp, header, fetched_at: int | None = None) -> VehicleRow:
        return VehicleRow(
            feed_timestamp=header.timestamp,
            vehicle_id=self._opt(vp.vehicle, "id"),
            vehicle_label=self._opt(vp.vehicle, "label"),
            trip_id=self._opt(vp.trip, "trip_id"),
            route_id=self._opt(vp.trip, "route_id"),
            direction_id=self._opt(vp.trip, "direction_id"),
            start_date=self._opt(vp.trip, "start_date"),
            start_time=self._opt(vp.trip, "start_time"),
            schedule_relationship=self._opt(
                vp.trip,
                "schedule_relationship",
                gtfs.TripDescriptor.ScheduleRelationship.Name,
            ),
            latitude=self._opt(vp.position, "latitude"),
            longitude=self._opt(vp.position, "longitude"),
            bearing=self._opt(vp.position, "bearing"),
            speed=self._opt(vp.position, "speed"),
            current_stop_sequence=self._opt(vp, "current_stop_sequence"),
            stop_id=self._opt(vp, "stop_id"),
            current_status=self._opt(
                vp, "current_status", gtfs.VehiclePosition.VehicleStopStatus.Name
            ),
            occupancy_status=self._opt(
                vp, "occupancy_status", gtfs.VehiclePosition.OccupancyStatus.Name
            ),
            occupancy_percentage=self._opt(vp, "occupancy_percentage"),
            vehicle_timestamp=self._opt(vp, "timestamp"),
        )

    def _decode_trip_update(
        self, tu, header, fetched_at: int | None = None
    ) -> Iterator[StopTimeUpdateRow]:
        trip_update_timestamp = self._opt(tu, "timestamp")
        trip_id = self._opt(tu.trip, "trip_id")
        route_id = self._opt(tu.trip, "route_id")
        direction_id = self._opt(tu.trip, "direction_id")
        start_date = self._opt(tu.trip, "start_date")
        start_time = self._opt(tu.trip, "start_time")
        schedule_relationship = self._opt(
            tu.trip,
            "schedule_relationship",
            gtfs.TripDescriptor.ScheduleRelationship.Name,
        )
        vehicle_id = self._opt(tu.vehicle, "id")
        vehicle_label = self._opt(tu.vehicle, "label")

        for stu in tu.stop_time_update:
            yield StopTimeUpdateRow(
                feed_timestamp=header.timestamp,
                trip_update_timestamp=trip_update_timestamp,
                trip_id=trip_id,
                route_id=route_id,
                direction_id=direction_id,
                start_date=start_date,
                start_time=start_time,
                schedule_relationship=schedule_relationship,
                vehicle_id=vehicle_id,
                vehicle_label=vehicle_label,
                stop_sequence=self._opt(stu, "stop_sequence"),
                stop_id=self._opt(stu, "stop_id"),
                arrival_delay=self._opt(stu.arrival, "delay"),
                arrival_time=self._opt(stu.arrival, "time"),
                arrival_uncertainty=self._opt(stu.arrival, "uncertainty"),
                departure_delay=self._opt(stu.departure, "delay"),
                departure_time=self._opt(stu.departure, "time"),
                departure_uncertainty=self._opt(stu.departure, "uncertainty"),
                stop_time_schedule_relationship=self._opt(
                    stu,
                    "schedule_relationship",
                    gtfs.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name,
                ),
            )

    def _decode_alert(
        self, sa, alert_id, header, fetched_at: int | None = None
    ) -> Iterator[AlertRow]:
        cause = self._opt(sa, "cause", gtfs.Alert.Cause.Name)
        effect = self._opt(sa, "effect", gtfs.Alert.Effect.Name)
        severity_level = self._opt(sa, "severity_level", gtfs.Alert.SeverityLevel.Name)
        header_text = self._translated_string(sa.header_text)
        description_text = self._translated_string(sa.description_text)
        url = self._translated_string(sa.url)

        for entity in sa.informed_entity:
            yield AlertRow(
                feed_timestamp=header.timestamp,
                alert_id=alert_id,
                cause=cause,
                effect=effect,
                severity_level=severity_level,
                header_text=header_text,
                description_text=description_text,
                url=url,
                agency_id=self._opt(entity, "agency_id"),
                route_id=self._opt(entity, "route_id"),
                route_type=self._opt(entity, "route_type"),
                direction_id=self._opt(entity, "direction_id"),
                trip_id=self._opt(entity.trip, "trip_id"),
                stop_id=self._opt(entity, "stop_id"),
            )

    @staticmethod
    def _translated_string(ts, language: str = "en") -> str | None:
        if not ts.translation:
            return None
        for t in ts.translation:
            if t.language == language:
                return t.text
        return ts.translation[0].text  # fall back to first available


@Decoder.register("mta_nyct")
class MTADecoder(StandardDecoder):
    def _decode_vehicle(self, vp, header, fetched_at: int | None = None) -> VehicleRow:
        base = super()._decode_vehicle(vp, header)
        # add NYCT-specific fields by reading vp's extensions
        return base


# TODO: schema-drift detection — compare incoming r.keys() against the expected
# set derived from MartaPredictionRow's dataclass fields. Log loudly (or raise) on
# unknown keys or missing required keys, so MARTA changing their API doesn't
# silently break the parse.
@Decoder.register("marta_json")
class MartaJsonDecoder(Decoder):
    produces: ClassVar[dict[type[Row], TableSpec]] = {
        MartaPredictionRow: TableSpec("marta_predictions")
    }

    def decode(self, parsed: list, *, fetched_at: int | None = None) -> Iterator[Row]:
        if fetched_at is None:
            raise ValueError("MartaJsonDecoder requires fetched_at")
        for r in parsed:
            event_dt = self._parse_event_time(r["EVENT_TIME"])  # → UTC datetime
            yield MartaPredictionRow(
                feed_timestamp=fetched_at,
                destination=r["DESTINATION"],
                direction=r["DIRECTION"],
                event_time=int(event_dt.timestamp()),
                is_realtime=(r["IS_REALTIME"].lower() == "true"),
                line=r["LINE"],
                next_arr=int(
                    self._combine_date_and_time(event_dt, r["NEXT_ARR"]).timestamp()
                ),
                station=r["STATION"],
                train_id=r["TRAIN_ID"],
                waiting_seconds=int(r["WAITING_SECONDS"]),
                waiting_time=r["WAITING_TIME"],
                delay=self._parse_delay(r.get("DELAY")),
                latitude=float(r["LATITUDE"]) if "LATITUDE" in r else None,
                longitude=float(r["LONGITUDE"]) if "LONGITUDE" in r else None,
            )

    @staticmethod
    def _parse_delay(delay: str | None) -> int | None:
        if delay is None:
            return None
        return int(delay.removeprefix("T").removesuffix("S"))

    @staticmethod
    def _parse_event_time(s: str) -> datetime:
        # "05/08/2026 4:35:20 PM" in America/New_York
        local = datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p")
        return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(
            timezone.utc
        )

    @staticmethod
    def _combine_date_and_time(event_dt: datetime, time_str: str) -> datetime:
        # "04:36:34 PM" — combine with event_dt's date, handle midnight rollover
        t = datetime.strptime(time_str, "%I:%M:%S %p").time()
        candidate = datetime.combine(
            event_dt.astimezone(ZoneInfo("America/New_York")).date(),
            t,
            tzinfo=ZoneInfo("America/New_York"),
        )
        # If candidate is more than ~12h before event, roll forward a day (midnight rollover)
        if (event_dt - candidate).total_seconds() > 12 * 3600:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
