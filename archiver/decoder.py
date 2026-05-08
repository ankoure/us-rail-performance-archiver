from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Iterator
from zoneinfo import ZoneInfo
from google.transit import gtfs_realtime_pb2 as gtfs
from google.protobuf.message import DecodeError as _ProtobufDecodeError


@dataclass
class VehicleRow:
    feed_timestamp: int | None = None
    vehicle_id: str | None = None
    vehicle_label: str | None = None
    trip_id: str | None = None
    route_id: str | None = None
    direction_id: int | None = None
    start_date: str | None = None
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
class StopTimeUpdateRow:
    feed_timestamp: int | None = None
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
class AlertRow:
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
    @abstractmethod
    def decode(self, raw: bytes) -> Iterator[VehicleRow | StopTimeUpdateRow | AlertRow]:
        raise NotImplementedError


class GtfsRtDecoder(Decoder):
    """Any decoder for the GTFS-RT protobuf format. Subclasses provide entity-level hooks."""

    def decode(self, raw: bytes) -> Iterator[VehicleRow | StopTimeUpdateRow | AlertRow]:
        feed = gtfs.FeedMessage()
        try:
            feed.ParseFromString(raw)
        except _ProtobufDecodeError as e:
            raise DecodeFailure(f"protobuf parse failed: {e}") from e
        for entity in feed.entity:
            if entity.HasField("vehicle"):
                yield self._decode_vehicle(entity.vehicle, feed.header)
            elif entity.HasField("trip_update"):
                yield from self._decode_trip_update(entity.trip_update, feed.header)
            elif entity.HasField("alert"):
                yield from self._decode_alert(entity.alert, entity.id, feed.header)

    @abstractmethod
    def _decode_vehicle(self, vp, header) -> VehicleRow:
        raise NotImplementedError

    @abstractmethod
    def _decode_trip_update(self, tu, header) -> Iterator[StopTimeUpdateRow]:
        raise NotImplementedError

    @abstractmethod
    def _decode_alert(self, sa, alert_id, header) -> Iterator[AlertRow]:
        raise NotImplementedError

    @staticmethod
    def _opt(msg, field: str, transform=None):
        if not msg.HasField(field):
            return None
        value = getattr(msg, field)
        return transform(value) if transform else value


class StandardDecoder(GtfsRtDecoder):
    def _decode_vehicle(self, vp, header) -> VehicleRow:
        return VehicleRow(
            feed_timestamp=header.timestamp,
            vehicle_id=self._opt(vp.vehicle, "id"),
            vehicle_label=self._opt(vp.vehicle, "label"),
            trip_id=self._opt(vp.trip, "trip_id"),
            route_id=self._opt(vp.trip, "route_id"),
            direction_id=self._opt(vp.trip, "direction_id"),
            start_date=self._opt(vp.trip, "start_date"),
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

    def _decode_trip_update(self, tu, header) -> Iterator[StopTimeUpdateRow]:
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

    def _decode_alert(self, sa, alert_id, header) -> Iterator[AlertRow]:
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


class MTADecoder(StandardDecoder):
    def _decode_vehicle(self, vp, header) -> VehicleRow:
        base = super()._decode_vehicle(vp, header)
        # add NYCT-specific fields by reading vp's extensions
        return base


class MartaJsonDecoder(Decoder):
    def decode(self, raw: bytes) -> Iterator[VehicleRow | StopTimeUpdateRow | AlertRow]:
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError as e:
            raise DecodeFailure(f"json parse failed: {e}") from e

        seen_trains: set[str] = set()

        for r in rows:
            trip_id = self._synthesize_trip_id(r)
            event_dt = self._parse_event_time(r["EVENT_TIME"])  # → UTC datetime

            # Vehicle: emit once per train per response
            train_id = r["TRAIN_ID"]
            if train_id not in seen_trains:
                seen_trains.add(train_id)
                yield VehicleRow(
                    feed_timestamp=int(event_dt.timestamp()),
                    vehicle_id=train_id,
                    vehicle_label=train_id,
                    trip_id=trip_id,
                    route_id=r["LINE"],
                    direction_id=self._direction_id(r["DIRECTION"]),
                    latitude=float(r["LATITUDE"]),
                    longitude=float(r["LONGITUDE"]),
                    vehicle_timestamp=int(event_dt.timestamp()),
                )

            # StopTimeUpdate: one per row
            yield StopTimeUpdateRow(
                feed_timestamp=int(event_dt.timestamp()),
                trip_id=trip_id,
                route_id=r["LINE"],
                direction_id=self._direction_id(r["DIRECTION"]),
                start_date=event_dt.strftime("%Y%m%d"),
                vehicle_id=train_id,
                vehicle_label=train_id,
                stop_id=r["STATION"],
                arrival_time=int(
                    self._combine_date_and_time(event_dt, r["NEXT_ARR"]).timestamp()
                ),
                arrival_delay=self._parse_delay(r["DELAY"]),
            )

    @staticmethod
    def _direction_id(direction: str) -> int:
        return 0 if direction in ("N", "E") else 1

    @staticmethod
    def _parse_delay(delay: str) -> int:
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

    @staticmethod
    def _synthesize_trip_id(r: dict) -> str:
        # Stable per (line, train, direction, day) — same trip_id should appear in every row of that train's predictions
        # this is a fragile heuristic; refine when you observe real behavior
        return f"{r['LINE']}-{r['TRAIN_ID']}-{r['DIRECTION']}-{r['EVENT_TIME'][:10]}"
