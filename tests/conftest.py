# tests/conftest.py
import pytest
from dataclasses import dataclass, field
from google.transit.gtfs_realtime_pb2 import (
    FeedMessage,
    FeedHeader,
    TripDescriptor,
    TripUpdate,
    VehiclePosition,
    Alert,
)
import time


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


def create_trip_updates(count: int, feed: FeedMessage) -> FeedMessage:
    # TODO: make this more robust
    for _ in range(count):
        entity = feed.entity.add()
        entity.id = "trip-entity-1"

        trip_update = entity.trip_update
        trip_update.trip.trip_id = "TRIP_001"
        trip_update.trip.route_id = "ROUTE_42"
        trip_update.trip.schedule_relationship = TripDescriptor.SCHEDULED

        # Add stop time updates
        stu = trip_update.stop_time_update.add()
        stu.stop_sequence = 5
        stu.stop_id = "STOP_123"
        stu.arrival.time = int(time.time()) + 120  # 2 min from now
        stu.departure.time = int(time.time()) + 150  # 2.5 min from now
        stu.schedule_relationship = TripUpdate.StopTimeUpdate.SCHEDULED
    return feed


def create_vehicle_positions(count: int, feed: FeedMessage) -> FeedMessage:
    # TODO: make this more robust
    for _ in range(count):
        entity2 = feed.entity.add()
        entity2.id = "vehicle-entity-1"

        vp = entity2.vehicle
        vp.trip.trip_id = "TRIP_001"
        vp.vehicle.id = "BUS_007"
        vp.vehicle.label = "007"
        vp.position.latitude = 42.3601
        vp.position.longitude = -71.0589
        vp.position.bearing = 180.0
        vp.position.speed = 12.5  # m/s
        vp.current_status = VehiclePosition.IN_TRANSIT_TO
        vp.timestamp = int(time.time())
    return feed


def create_service_alert_messages(count: int, feed: FeedMessage) -> FeedMessage:
    for _ in range(count):
        # Add an Alert entity
        entity = feed.entity.add()
        entity.id = "alert-1"

        alert = entity.alert

        # --- What's affected ---
        selector = alert.informed_entity.add()
        selector.agency_id = "MBTA"  # whole agency, or narrow it down:
        selector.route_id = "Red"  # specific route
        selector.stop_id = "place-pktrm"  # specific stop (optional)
        # selector.trip.trip_id = "TRIP_001" # or a specific trip

        # --- Active time window(s) ---
        period = alert.active_period.add()
        period.start = int(time.time())
        period.end = int(time.time()) + 3600  # active for 1 hour

        # --- Cause & Effect ---
        alert.cause = Alert.CONSTRUCTION
        alert.effect = Alert.REDUCED_SERVICE

        # --- Human-readable text ---
        # Header (short title)
        header_text = alert.header_text.translation.add()
        header_text.text = "Red Line delays at Park Street"
        header_text.language = "en"

        # Description (full detail)
        desc_text = alert.description_text.translation.add()
        desc_text.text = "Due to construction, expect 10-15 minute delays on the Red Line through Park Street station."
        desc_text.language = "en"

        # URL for more info (optional)
        url_text = alert.url.translation.add()
        url_text.text = "https://mbta.com/alerts/red-line"
        url_text.language = "en"

    return feed


@pytest.fixture
def make_response():
    """Factory fixture — call it inside a test to build a FakeResponse."""

    def _make(status_code=200, headers=None, content=b""):
        return _FakeResponse(
            status_code=status_code,
            headers=headers or {},
            content=content,
        )

    return _make


@pytest.fixture
def valid_protobuf_bytes():
    msg = FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    return msg.SerializeToString()


@pytest.fixture
def create_mixed_protobuf():
    """Factory fixture — call it inside a test to build a mixed FeedMessage."""

    def _create(
        vehicle_position_message_count: int = 1,
        trip_update_message_count: int = 1,
        service_alert_message_count: int = 1,
    ) -> bytes:
        feed = FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = FeedHeader.FULL_DATASET
        feed.header.timestamp = int(time.time())

        feed = create_vehicle_positions(vehicle_position_message_count, feed)
        feed = create_trip_updates(trip_update_message_count, feed)
        feed = create_service_alert_messages(service_alert_message_count, feed)

        return feed.SerializeToString()

    return _create
