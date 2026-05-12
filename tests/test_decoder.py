from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs

from archiver.decoder import (
    GtfsRtDecoder,
    MartaJsonDecoder,
    MartaPredictionRow,
    StandardDecoder,
    MTADecoder,
    VehicleRow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_feed(
    feed_timestamp: int = 1_700_000_000,
    vehicle_id: str = "v1",
    vehicle_label: str = "Bus 42",
    trip_id: str = "trip-1",
    route_id: str = "route-A",
    direction_id: int = 0,
    start_date: str = "20240101",
    latitude: float = 40.7128,
    longitude: float = -74.0060,
    bearing: float = 90.0,
    speed: float = 12.5,
    current_stop_sequence: int = 3,
    stop_id: str = "stop-99",
    current_status: int = gtfs.VehiclePosition.IN_TRANSIT_TO,
    schedule_relationship: int = gtfs.TripDescriptor.SCHEDULED,
    occupancy_status: int = gtfs.VehiclePosition.FEW_SEATS_AVAILABLE,
    occupancy_percentage: int = 65,
    vehicle_timestamp: int = 1_700_000_050,
) -> gtfs.FeedMessage:
    """Return a FeedMessage with a single fully-populated vehicle entity."""
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_timestamp

    entity = feed.entity.add()
    entity.id = "ent-1"

    vp = entity.vehicle
    vp.vehicle.id = vehicle_id
    vp.vehicle.label = vehicle_label

    vp.trip.trip_id = trip_id
    vp.trip.route_id = route_id
    vp.trip.direction_id = direction_id
    vp.trip.start_date = start_date
    vp.trip.schedule_relationship = schedule_relationship

    vp.position.latitude = latitude
    vp.position.longitude = longitude
    vp.position.bearing = bearing
    vp.position.speed = speed

    vp.current_stop_sequence = current_stop_sequence
    vp.stop_id = stop_id
    vp.current_status = current_status
    vp.occupancy_status = occupancy_status
    vp.occupancy_percentage = occupancy_percentage
    vp.timestamp = vehicle_timestamp

    return feed


def make_minimal_feed(feed_timestamp: int = 1_700_000_000) -> gtfs.FeedMessage:
    """Return a FeedMessage with only required / always-present fields set."""
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_timestamp

    entity = feed.entity.add()
    entity.id = "ent-min"

    # vehicle sub-message must exist for HasField("vehicle") to be True
    entity.vehicle.SetInParent()

    return feed


# ---------------------------------------------------------------------------
# StandardDecoder – full entity
# ---------------------------------------------------------------------------


class TestStandardDecoderFullEntity:
    decoder = StandardDecoder()

    @pytest.fixture(scope="class")
    def pos(self):
        feed = make_feed()
        results = list(self.decoder.decode(feed))

        assert len(results) == 1
        return results[0]

    def test_returns_vehicle_position(self, pos):
        assert isinstance(pos, VehicleRow)

    def test_feed_timestamp(self, pos):
        assert pos.feed_timestamp == 1_700_000_000

    def test_vehicle_id(self, pos):
        assert pos.vehicle_id == "v1"

    def test_vehicle_label(self, pos):
        assert pos.vehicle_label == "Bus 42"

    def test_trip_id(self, pos):
        assert pos.trip_id == "trip-1"

    def test_route_id(self, pos):
        assert pos.route_id == "route-A"

    def test_direction_id(self, pos):
        assert pos.direction_id == 0

    def test_start_date(self, pos):
        assert pos.start_date == "20240101"

    def test_latitude(self, pos):
        assert pos.latitude == pytest.approx(40.7128)

    def test_longitude(self, pos):
        assert pos.longitude == pytest.approx(-74.0060)

    def test_bearing(self, pos):
        assert pos.bearing == pytest.approx(90.0)

    def test_speed(self, pos):
        assert pos.speed == pytest.approx(12.5)

    def test_current_stop_sequence(self, pos):
        assert pos.current_stop_sequence == 3

    def test_stop_id(self, pos):
        assert pos.stop_id == "stop-99"

    def test_current_status(self, pos):
        # The transform returns the enum *class* called with the int value;
        # verify it round-trips to the expected enum string.
        assert pos.current_status == "IN_TRANSIT_TO"

    def test_schedule_relationship(self, pos):
        assert pos.schedule_relationship == "SCHEDULED"

    def test_occupancy_status(self, pos):
        assert pos.occupancy_status == "FEW_SEATS_AVAILABLE"

    def test_occupancy_percentage(self, pos):
        assert pos.occupancy_percentage == 65

    def test_vehicle_timestamp(self, pos):
        assert pos.vehicle_timestamp == 1_700_000_050


# ---------------------------------------------------------------------------
# StandardDecoder – optional / absent fields become None
# ---------------------------------------------------------------------------


class TestStandardDecoderOptionalFields:
    decoder = StandardDecoder()

    def _decode_minimal(self):
        feed = make_minimal_feed()
        return list(self.decoder.decode(feed))[0]

    def test_missing_vehicle_id_is_none(self):
        pos = self._decode_minimal()
        assert pos.vehicle_id is None

    def test_missing_vehicle_label_is_none(self):
        pos = self._decode_minimal()
        assert pos.vehicle_label is None

    def test_missing_trip_id_is_none(self):
        pos = self._decode_minimal()
        assert pos.trip_id is None

    def test_missing_route_id_is_none(self):
        pos = self._decode_minimal()
        assert pos.route_id is None

    def test_missing_latitude_is_none(self):
        pos = self._decode_minimal()
        assert pos.latitude is None

    def test_missing_stop_id_is_none(self):
        pos = self._decode_minimal()
        assert pos.stop_id is None

    def test_missing_occupancy_percentage_is_none(self):
        pos = self._decode_minimal()
        assert pos.occupancy_percentage is None

    def test_missing_vehicle_timestamp_is_none(self):
        pos = self._decode_minimal()
        assert pos.vehicle_timestamp is None


# ---------------------------------------------------------------------------
# Decoder.decode – entity-level filtering
# ---------------------------------------------------------------------------


class TestDecoderEntityFiltering:
    decoder = StandardDecoder()

    def test_empty_feed_yields_nothing(self):
        feed = gtfs.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 0
        assert list(self.decoder.decode(feed)) == []

    def test_non_vehicle_entity_is_skipped(self):
        """A feed with only a trip_update entity should yield no VehiclePositions."""
        feed = gtfs.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 0

        entity = feed.entity.add()
        entity.id = "tu-1"
        tu = entity.trip_update
        tu.trip.trip_id = "trip-x"

        assert list(self.decoder.decode(feed)) == []

    def test_multiple_vehicle_entities_all_decoded(self):
        feed = gtfs.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 1_700_000_000

        for i in range(3):
            entity = feed.entity.add()
            entity.id = f"ent-{i}"
            entity.vehicle.vehicle.id = f"v{i}"

        results = list(self.decoder.decode(feed))

        assert len(results) == 3
        assert [r.vehicle_id for r in results] == ["v0", "v1", "v2"]

    def test_mixed_entities_only_vehicles_decoded(self):
        """Feed with one vehicle and one trip_update should yield exactly one result."""
        feed = gtfs.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 1_700_000_000

        # vehicle entity
        v_ent = feed.entity.add()
        v_ent.id = "v-ent"
        v_ent.vehicle.vehicle.id = "bus-1"

        # trip_update entity (should be ignored)
        tu_ent = feed.entity.add()
        tu_ent.id = "tu-ent"
        tu_ent.trip_update.trip.trip_id = "trip-ignored"

        results = list(self.decoder.decode(feed))

        assert len(results) == 1
        assert results[0].vehicle_id == "bus-1"


# ---------------------------------------------------------------------------
# GtfsRtDecoder._opt static method
# ---------------------------------------------------------------------------


class TestOptHelper:
    """Unit-test _opt directly without constructing full feeds."""

    def _build_position(self, lat: float, lon: float):
        """Return a Position protobuf with lat/lon set."""
        vp = gtfs.VehiclePosition()
        vp.position.latitude = lat
        vp.position.longitude = lon
        return vp.position

    def test_present_field_returned(self):
        pos = self._build_position(1.0, 2.0)
        assert GtfsRtDecoder._opt(pos, "latitude") == pytest.approx(1.0)

    def test_absent_field_returns_none(self):
        pos = self._build_position(1.0, 2.0)
        # bearing is not set
        assert GtfsRtDecoder._opt(pos, "bearing") is None

    def test_transform_applied_when_present(self):
        pos = self._build_position(1.0, 2.0)
        result = GtfsRtDecoder._opt(pos, "latitude", lambda v: v * 2)
        assert result == pytest.approx(2.0)

    def test_transform_not_called_when_absent(self):
        called = []
        pos = self._build_position(1.0, 2.0)
        GtfsRtDecoder._opt(pos, "bearing", lambda v: called.append(v) or v)
        assert called == []


# ---------------------------------------------------------------------------
# MTADecoder – smoke test (subclass parity)
# ---------------------------------------------------------------------------


class TestMTADecoder:
    decoder = MTADecoder()

    def test_returns_vehicle_position(self):
        feed = make_feed()
        results = list(self.decoder.decode(feed))

        assert len(results) == 1
        assert isinstance(results[0], VehicleRow)

    def test_base_fields_preserved(self):
        """MTADecoder currently delegates to StandardDecoder; spot-check a few fields."""
        feed = make_feed(vehicle_id="mta-bus", route_id="M15")
        pos = list(self.decoder.decode(feed))[0]
        assert pos.vehicle_id == "mta-bus"
        assert pos.route_id == "M15"


def test_marta_decode_smoke():
    parsed = [
        {
            "DESTINATION": "Airport",
            "DIRECTION": "S",
            "EVENT_TIME": "05/08/2026 4:35:20 PM",
            "IS_REALTIME": "true",
            "LINE": "RED",
            "NEXT_ARR": "04:36:34 PM",
            "STATION": "DUNWOODY STATION",
            "TRAIN_ID": "411",
            "WAITING_SECONDS": "64",
            "WAITING_TIME": "1 min",
            "DELAY": "T197S",
            "LATITUDE": "33.91",
            "LONGITUDE": "-84.35",
        }
    ]
    ts = int(datetime.now(timezone.utc).timestamp())
    rows = list(MartaJsonDecoder().decode(parsed, fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, MartaPredictionRow)
    assert row.feed_timestamp == ts
    assert row.destination == "Airport"
    assert row.direction == "S"
    # assert row.event_time == ts
    assert row.is_realtime is True
    assert row.line == "RED"
    # assert row.next_arr == ts
    assert row.station == "DUNWOODY STATION"
    assert row.train_id == "411"
    assert row.waiting_seconds == 64
    assert row.waiting_time == "1 min"
    assert row.delay == 197
    assert row.latitude == 33.91
    assert row.longitude == -84.35
    assert isinstance(row.event_time, int)
    assert row.event_time > 0
    assert isinstance(row.next_arr, int)
    assert (
        row.next_arr >= row.event_time
    )  # predicted arrival is at or after observation


def test_parse_event_time_dst():
    result = MartaJsonDecoder._parse_event_time("05/08/2026 4:35:20 PM")
    expected = datetime(
        2026, 5, 8, 16, 35, 20, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(timezone.utc)
    assert result == expected


def test_combine_date_and_time_same_day():
    event_dt = datetime(2026, 5, 8, 20, 35, 20, tzinfo=timezone.utc)  # 4:35 PM EDT
    result = MartaJsonDecoder._combine_date_and_time(event_dt, "04:36:34 PM")
    assert result == datetime(2026, 5, 8, 20, 36, 34, tzinfo=timezone.utc)


def test_combine_date_and_time_midnight_rollover():
    # event at 11:55 PM EDT on May 8; prediction "12:05 AM" should roll forward to May 9
    event_dt = datetime(2026, 5, 9, 3, 55, 0, tzinfo=timezone.utc)  # 11:55 PM EDT May 8
    result = MartaJsonDecoder._combine_date_and_time(event_dt, "12:05:00 AM")
    assert result == datetime(
        2026, 5, 9, 4, 5, 0, tzinfo=timezone.utc
    )  # 12:05 AM EDT May 9
