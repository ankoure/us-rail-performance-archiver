from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs

from archiver.decoder import (
    Decoder,
    GtfsRtDecoder,
    LirrJsonDecoder,
    LirrTrainRow,
    MartaJsonDecoder,
    MartaPredictionRow,
    MwrtaJsonDecoder,
    MwrtaVehicleRow,
    RouteMatchJsonDecoder,
    RouteMatchVehicleRow,
    StandardDecoder,
    SwivJsonDecoder,
    SwivVehicleRow,
    TrilliumJsonDecoder,
    TrilliumVehicleRow,
    VtaJsonDecoder,
    VtaVehicleRow,
    PassioGoDecoder,
    PassioVehicleRow,
    StopTimeUpdateRow,
    MTADecoder,
    VehicleRow,
    validate_record_keys,
)
from dataclasses import fields as _dc_fields

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
    start_time: str = "08:15:00",
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
    vp.trip.start_time = start_time
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

    def test_start_time(self, pos):
        assert pos.start_time == "08:15:00"

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

    def test_missing_start_time_is_none(self):
        pos = self._decode_minimal()
        assert pos.start_time is None


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


_MARTA_BASELINE = {
    "EVENT_TIME": "05/08/2026 4:35:20 PM",
    "DESTINATION": "Airport",
    "DIRECTION": "S",
    "IS_REALTIME": "true",
    "LINE": "RED",
    "NEXT_ARR": "04:36:34 PM",
    "STATION": "DUNWOODY STATION",
    "TRAIN_ID": "411",
    "WAITING_SECONDS": "64",
    "WAITING_TIME": "1 min",
}


class TestValidateRecordKeys:
    def test_baseline_returns_none(self):
        assert validate_record_keys(_MARTA_BASELINE, MartaPredictionRow) is None

    def test_optional_keys_present_returns_none(self):
        record = {**_MARTA_BASELINE, "DELAY": "T197S", "LATITUDE": "33.91"}
        assert validate_record_keys(record, MartaPredictionRow) is None

    def test_extra_key_reported(self):
        record = {**_MARTA_BASELINE, "NEW_FIELD": "x"}
        report = validate_record_keys(record, MartaPredictionRow)
        assert report is not None
        assert report.extras == frozenset({"NEW_FIELD"})
        assert report.missing_required == frozenset()
        assert not report.has_missing_required

    def test_missing_required_reported(self):
        record = {k: v for k, v in _MARTA_BASELINE.items() if k != "STATION"}
        report = validate_record_keys(record, MartaPredictionRow)
        assert report is not None
        assert report.missing_required == frozenset({"STATION"})
        assert report.has_missing_required

    def test_missing_optional_returns_none(self):
        assert validate_record_keys(_MARTA_BASELINE, MartaPredictionRow) is None

    def test_rename_reported_as_both(self):
        record = {k: v for k, v in _MARTA_BASELINE.items() if k != "EVENT_TIME"}
        record["EVT_TIME"] = "x"
        report = validate_record_keys(record, MartaPredictionRow)
        assert report is not None
        assert report.missing_required == frozenset({"EVENT_TIME"})
        assert report.extras == frozenset({"EVT_TIME"})


class TestMartaJsonDecoderValidate:
    decoder = MartaJsonDecoder()

    def test_empty_payload(self):
        assert self.decoder.validate([]) is None

    def test_valid_payload(self):
        assert self.decoder.validate([_MARTA_BASELINE]) is None

    def test_drifted_first_record(self):
        record = {k: v for k, v in _MARTA_BASELINE.items() if k != "LINE"}
        report = self.decoder.validate([record])
        assert report is not None
        assert report.missing_required == frozenset({"LINE"})


class TestMartaJsonDecoderSkipsDriftedRecords:
    decoder = MartaJsonDecoder()

    def test_drifted_record_skipped(self):
        broken = {k: v for k, v in _MARTA_BASELINE.items() if k != "STATION"}
        ts = int(datetime.now(timezone.utc).timestamp())
        rows = list(self.decoder.decode([broken, _MARTA_BASELINE], fetched_at=ts))
        assert len(rows) == 1
        assert rows[0].station == "DUNWOODY STATION"

    def test_all_drifted_yields_nothing(self):
        broken = {k: v for k, v in _MARTA_BASELINE.items() if k != "EVENT_TIME"}
        ts = int(datetime.now(timezone.utc).timestamp())
        assert list(self.decoder.decode([broken, broken], fetched_at=ts)) == []


# ---------------------------------------------------------------------------
# LIRR (MyLIRR locations JSON) decoder
# ---------------------------------------------------------------------------

# Mirrors the real backend-unified.mylirr.org/locations shape: all ten top-level
# keys present, nested location/status/details, native (non-string) values, and a
# stops list with a DEPARTED stop followed by the current (EN_ROUTE) one.
_LIRR_BASELINE = {
    "train_id": "LIRR_2026-06-16_64",
    "railroad": "LIRR",
    "run_date": "2026-06-16",
    "train_num": "64",
    "realtime": True,
    "consist": {"fleet": "LIRR_DIESEL", "cars": []},
    "alerts": [],
    "location": {
        "latitude": 40.70894,
        "longitude": -73.299726,
        "heading": 71.6,
        "speed": 51.7,  # mph, raw
        "timestamp": 1781620580,
        "source": "GPS",
    },
    "status": {"canceled": False, "held": False, "otp": -97, "otp_location": "BAB"},
    "details": {
        "headsign": "Patchogue",
        "branch": "Montauk",
        "stops": [
            {
                "code": "BTA",
                "stop_status": "DEPARTED",
                "sched_time": 1781620320,
                "act_depart_time": 1781620362,
            },
            {"code": "BSR", "stop_status": "EN_ROUTE", "sched_time": 1781620680},
        ],
    },
}


def test_lirr_decode_smoke():
    ts = 1781620580
    rows = list(LirrJsonDecoder().decode([_LIRR_BASELINE], fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, LirrTrainRow)
    assert row.feed_timestamp == ts
    # raw fidelity: identifiers and units are passed through untouched
    assert row.train_num == "64"  # raw string, not rewritten to a static trip_id
    assert row.speed_mph == 51.7  # raw mph, NOT converted to m/s (~23.1)
    assert row.realtime is True
    assert row.canceled is False
    assert row.latitude == 40.70894
    assert row.longitude == -73.299726
    assert row.heading == 71.6
    assert row.location_timestamp == 1781620580
    # current stop = first non-DEPARTED stop, raw code/status preserved
    assert row.current_stop_code == "BSR"
    assert row.current_stop_status == "EN_ROUTE"
    assert row.current_stop_seq == 2
    assert row.act_arrive_time is None
    assert row.act_depart_time is None


def test_lirr_decode_all_stops_departed_has_no_current_stop():
    train = {
        **_LIRR_BASELINE,
        "details": {
            "stops": [{"code": "BTA", "stop_status": "DEPARTED"}],
        },
    }
    rows = list(LirrJsonDecoder().decode([train], fetched_at=1781620580))
    assert rows[0].current_stop_code is None
    assert rows[0].current_stop_seq is None


def test_lirr_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(LirrJsonDecoder().decode([_LIRR_BASELINE]))


class TestLirrValidateRecordKeys:
    def test_baseline_returns_none(self):
        assert validate_record_keys(_LIRR_BASELINE, LirrTrainRow) is None

    def test_extra_key_reported(self):
        record = {**_LIRR_BASELINE, "new_field": "x"}
        report = validate_record_keys(record, LirrTrainRow)
        assert report is not None
        assert report.extras == frozenset({"new_field"})
        assert not report.has_missing_required

    def test_missing_required_reported(self):
        record = {k: v for k, v in _LIRR_BASELINE.items() if k != "location"}
        report = validate_record_keys(record, LirrTrainRow)
        assert report is not None
        assert report.missing_required == frozenset({"location"})
        assert report.has_missing_required

    def test_missing_optional_returns_none(self):
        record = {k: v for k, v in _LIRR_BASELINE.items() if k != "alerts"}
        assert validate_record_keys(record, LirrTrainRow) is None


class TestLirrJsonDecoderValidate:
    decoder = LirrJsonDecoder()

    def test_empty_payload(self):
        assert self.decoder.validate([]) is None

    def test_valid_payload(self):
        assert self.decoder.validate([_LIRR_BASELINE]) is None

    def test_drifted_first_record(self):
        record = {k: v for k, v in _LIRR_BASELINE.items() if k != "details"}
        report = self.decoder.validate([record])
        assert report is not None
        assert report.missing_required == frozenset({"details"})


class TestLirrJsonDecoderSkipsDriftedRecords:
    decoder = LirrJsonDecoder()

    def test_drifted_record_skipped(self):
        broken = {k: v for k, v in _LIRR_BASELINE.items() if k != "status"}
        rows = list(
            self.decoder.decode([broken, _LIRR_BASELINE], fetched_at=1781620580)
        )
        assert len(rows) == 1
        assert rows[0].train_num == "64"


# ---------------------------------------------------------------------------
# MWRTA vehicle JSON decoder (shared by MWRTA and CCRTA, which have divergent
# key sets: required_input_keys is their intersection, optional their union)
# ---------------------------------------------------------------------------

# MWRTA-shaped record: has Active/Destination/Address*, no RouteName/Direction.
_MWRTA_BASELINE = {
    "ID": 999903293,
    "ScheduleDelta": None,
    "Route": "RT04S",  # MWRTA sends Route as a string
    "Destination": None,
    "Lat": 42.2743123,
    "Long": -71.4135038,
    "Speed": 20.383,  # raw units, no conversion
    "Heading": 109.59,
    "DateTime": "2026-06-16T11:14:40",  # naive local ISO string, kept raw
    "VehiclePlate": "174",
    "SequenceId": None,
    "Active": True,
    "NumberOfSatelites": 4,
    "FixStrength": 4,
    "Mode": None,
    "AddressStreet": "171 Irving St",
    "AddressCity": "Framingham",
}

# CCRTA-shaped record: has RouteName/DirectionName, no Active/Destination, and
# Route is an int (604) rather than a string.
_CCRTA_BASELINE = {
    "ID": 10942780,
    "Route": 604,
    "Lat": 41.7883,
    "Long": -69.9914,
    "Speed": 0.0,
    "Heading": 0.0,
    "DateTime": "2026-06-16T11:13:55",
    "VehiclePlate": "110",
    "NumberOfSatelites": 4,
    "FixStrength": 4,
    "Mode": None,
    "SequenceId": None,
    "RouteName": "Flex",
    "DirectionName": "in",
}


def test_mwrta_decode_smoke():
    ts = 1781622000
    rows = list(MwrtaJsonDecoder().decode([_MWRTA_BASELINE], fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, MwrtaVehicleRow)
    assert row.feed_timestamp == ts
    assert row.vehicle_id == "999903293"
    assert row.route == "RT04S"
    assert row.speed == 20.383  # raw, not converted
    assert row.vehicle_datetime == "2026-06-16T11:14:40"  # raw string, not parsed
    assert row.latitude == 42.2743123
    assert row.active is True


def test_mwrta_decoder_handles_ccrta_shape():
    rows = list(MwrtaJsonDecoder().decode([_CCRTA_BASELINE], fetched_at=1781622000))
    row = rows[0]
    assert row.route == "604"  # int coerced to str so the column type is stable
    assert row.route_name == "Flex"
    assert row.direction_name == "in"
    assert row.active is None  # CCRTA omits Active


def test_mwrta_decode_skips_null_elements():
    ts = 1781622000  # same fetched_at the smoke test uses
    rows = list(MwrtaJsonDecoder().decode([_MWRTA_BASELINE, None], fetched_at=ts))
    assert len(rows) == 1  # the null is dropped, the real record survives


def test_mwrta_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(MwrtaJsonDecoder().decode([_MWRTA_BASELINE]))


class TestMwrtaValidateRecordKeys:
    def test_mwrta_baseline_returns_none(self):
        assert validate_record_keys(_MWRTA_BASELINE, MwrtaVehicleRow) is None

    def test_ccrta_baseline_returns_none(self):
        # The divergent CCRTA shape must also validate against the shared row.
        assert validate_record_keys(_CCRTA_BASELINE, MwrtaVehicleRow) is None

    def test_missing_required_reported(self):
        record = {k: v for k, v in _MWRTA_BASELINE.items() if k != "Lat"}
        report = validate_record_keys(record, MwrtaVehicleRow)
        assert report is not None
        assert report.missing_required == frozenset({"Lat"})

    def test_extra_key_reported(self):
        record = {**_MWRTA_BASELINE, "NewField": "x"}
        report = validate_record_keys(record, MwrtaVehicleRow)
        assert report is not None
        assert report.extras == frozenset({"NewField"})


class TestMwrtaJsonDecoderValidate:
    decoder = MwrtaJsonDecoder()

    def test_empty_payload(self):
        assert self.decoder.validate([]) is None

    def test_valid_payloads(self):
        assert self.decoder.validate([_MWRTA_BASELINE]) is None
        assert self.decoder.validate([_CCRTA_BASELINE]) is None

    def test_drifted_record_skipped(self):
        broken = {k: v for k, v in _MWRTA_BASELINE.items() if k != "ID"}
        rows = list(
            self.decoder.decode([broken, _MWRTA_BASELINE], fetched_at=1781622000)
        )
        assert len(rows) == 1
        assert rows[0].vehicle_id == "999903293"


# ---------------------------------------------------------------------------
# RouteMatch (BRTA) JSON decoder — payload is a {"data": [...]} envelope, and
# fields arrive as native numbers (heading can be present-but-null)
# ---------------------------------------------------------------------------

_BRTA_RECORD = {
    "blockId": None,
    "currentPassengers": 5,
    "deadhead": False,
    "heading": None,  # present but null — must not crash
    "headingName": "NORTH",
    "internalDriverId": "0257",
    "internalVehicleId": "1959",
    "landRouteId": 1935,
    "lastTimePointCrossedDate": "2026-06-16T11:54:36.000-04:00",
    "lastTimePointCrossedId": "Cheshire Ctr on Railroad St ",
    "lastUpdate": "2026-06-16T12:06:50.000-04:00",
    "latitude": 42.46828842163086,
    "longitude": -73.2047119140625,
    "masterRouteDescription": "Rte 01 ",
    "masterRouteId": "Wk Rt 01",
    "masterRouteLongName": "Rte 01 ",
    "masterRouteShortName": "Rte 01 ",
    "onRouteEdgeId": 0,
    "onRouteEdgePercent": 0.0,
    "onRouteLatitude": 0.0,
    "onRouteLongitude": 0.0,
    "percentFull": 16,
    "scheduleAdherence": -4,
    "showVehicleCapacity": False,
    "speed": 0,
    "subRouteDescription": "Walmart North Adams",
    "subRouteLongName": "Wk Rt 01 inbound standard",
    "subRouteShortName": "Wk Rt 01 inbound standard",
    "templates": {"body": "Heading Inbound on Rte 01 ", "title": "1959"},
    "totalCapacity": 30,
    "tripDirection": "Inbound",
    "tripId": "Rte 01 1130 in",
    "vehicleId": "1959",
}
_BRTA_ENVELOPE = {"data": [_BRTA_RECORD], "feedVersion": "1", "fromCache": False}


def test_routematch_decode_smoke():
    ts = 1781622000
    rows = list(RouteMatchJsonDecoder().decode(_BRTA_ENVELOPE, fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, RouteMatchVehicleRow)
    assert row.feed_timestamp == ts
    assert row.vehicle_id == "1959"
    assert row.route_id == "Wk Rt 01"  # raw, no normalization
    assert row.trip_id == "Rte 01 1130 in"  # raw
    assert row.last_update == "2026-06-16T12:06:50.000-04:00"  # raw string
    assert row.heading is None  # present-but-null handled
    assert row.deadhead is False


def test_routematch_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(RouteMatchJsonDecoder().decode(_BRTA_ENVELOPE))


class TestRouteMatchJsonDecoderValidate:
    decoder = RouteMatchJsonDecoder()

    def test_empty_data_returns_none(self):
        assert self.decoder.validate({"data": []}) is None

    def test_valid_envelope_returns_none(self):
        # Validation must look INSIDE data, not at the envelope keys.
        assert self.decoder.validate(_BRTA_ENVELOPE) is None

    def test_drifted_record_reported(self):
        broken = {k: v for k, v in _BRTA_RECORD.items() if k != "vehicleId"}
        report = self.decoder.validate({"data": [broken]})
        assert report is not None
        assert report.missing_required == frozenset({"vehicleId"})


# ---------------------------------------------------------------------------
# Trillium (MeVa) JSON decoder — {"status","data":[...]} envelope OR bare array;
# heading is a cardinal string, headingDegrees is the numeric bearing
# ---------------------------------------------------------------------------

_MEVA_RECORD = {
    "patternId": 28965,
    "capacity": 31,
    "id": 8841,
    "lat": 42.76293055062878,
    "lon": -71.03979440703363,
    "name": "1504",
    "passengerLoad": 0.29,
    "lastUpdated": "2026-06-16T15:14:54Z",
    "heading": "SE",  # cardinal string
    "speed": 9,
    "headingDegrees": 151.48535020255446,  # numeric bearing
    "shapeDistanceTraveled": 5069.048978752347,
    "route_short_name": "18",
    "vehicleType": "bus",
    "route_id": "10730",
}
_MEVA_ENVELOPE = {"status": "OK", "data": [_MEVA_RECORD]}


def test_trillium_decode_smoke():
    ts = 1781622000
    rows = list(TrilliumJsonDecoder().decode(_MEVA_ENVELOPE, fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, TrilliumVehicleRow)
    assert row.feed_timestamp == ts
    assert row.vehicle_id == "8841"
    assert row.route_id == "10730"  # raw
    assert row.heading == "SE"  # cardinal string, kept distinct from...
    assert row.heading_degrees == 151.48535020255446  # ...the numeric bearing
    assert row.last_updated == "2026-06-16T15:14:54Z"  # raw


def test_trillium_decode_accepts_bare_array():
    # Some Trillium endpoints return a bare list rather than the envelope.
    rows = list(TrilliumJsonDecoder().decode([_MEVA_RECORD], fetched_at=1781622000))
    assert len(rows) == 1
    assert rows[0].vehicle_id == "8841"


def test_trillium_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(TrilliumJsonDecoder().decode(_MEVA_ENVELOPE))


class TestTrilliumJsonDecoderValidate:
    decoder = TrilliumJsonDecoder()

    def test_empty_returns_none(self):
        assert self.decoder.validate({"data": []}) is None
        assert self.decoder.validate([]) is None

    def test_valid_envelope_and_array(self):
        assert self.decoder.validate(_MEVA_ENVELOPE) is None
        assert self.decoder.validate([_MEVA_RECORD]) is None

    def test_drifted_record_reported(self):
        broken = {k: v for k, v in _MEVA_RECORD.items() if k != "id"}
        report = self.decoder.validate({"data": [broken]})
        assert report is not None
        assert report.missing_required == frozenset({"id"})

    def test_drifted_record_skipped_in_decode(self):
        broken = {k: v for k, v in _MEVA_RECORD.items() if k != "id"}
        rows = list(
            self.decoder.decode({"data": [broken, _MEVA_RECORD]}, fetched_at=1781622000)
        )
        assert len(rows) == 1
        assert rows[0].vehicle_id == "8841"


# ---------------------------------------------------------------------------
# Cross-cutting checks for the new JSON decoders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,row_cls",
    [
        ("mta_lirr_json", LirrTrainRow),
        ("mwrta_json", MwrtaVehicleRow),
        ("routematch_json", RouteMatchVehicleRow),
        ("trillium_json", TrilliumVehicleRow),
        ("swiv_json", SwivVehicleRow),
        ("vta_json", VtaVehicleRow),
    ],
)
def test_new_json_decoders_registered(name, row_cls):
    decoder = Decoder.from_name(name)
    # the decoder name resolves and advertises the expected output row/table
    assert row_cls in decoder.produces
    assert decoder.produces[row_cls].name  # a non-empty parquet table name


def test_routematch_drifted_record_skipped_in_decode():
    broken = {k: v for k, v in _BRTA_RECORD.items() if k != "vehicleId"}
    rows = list(
        RouteMatchJsonDecoder().decode(
            {"data": [broken, _BRTA_RECORD]}, fetched_at=1781622000
        )
    )
    assert len(rows) == 1
    assert rows[0].vehicle_id == "1959"


# ---------------------------------------------------------------------------
# Swiv (GATRA/LRTA/WRTA) JSON decoder — {"vehicule":[...]} envelope with nested
# localisation/conduite objects; LRTA omits tauxRemplissage/estAffichable
# ---------------------------------------------------------------------------

# GATRA-shaped: has tauxRemplissage + estAffichable.
_GATRA_RECORD = {
    "localisation": {"lat": 41.972609774719324, "lng": -70.71297321335793, "cap": 191},
    "conduite": {
        "idLigne": 16103,
        "vitesse": 13,
        "destination": "Plymouth Center",
        "avanceRetard": "on time",
        "arretSuiv": {"nomCommercial": "KingstonCollect", "estimationTemps": 1},
    },
    "id": 1017,
    "tauxRemplissage": 10,
    "estAffichable": True,
    "vehiculeLoad": "10%",
    "type": "NEW FLYER",
    "numeroEquipement": "1590",
}
# LRTA-shaped: no tauxRemplissage / estAffichable.
_LRTA_RECORD = {
    "localisation": {"lat": 42.582055142084734, "lng": -71.19561524469265, "cap": 327},
    "conduite": {
        "idLigne": 27302,
        "vitesse": 0,
        "destination": "Kennedy Center",
        "avanceRetard": "6 min late",
        "arretSuiv": {"nomCommercial": "Main&Hill", "estimationTemps": 1},
    },
    "id": 6007,
    "vehiculeLoad": "27%",
    "type": "Bus",
    "numeroEquipement": "2508",
}


def test_swiv_decode_smoke():
    ts = 1781622000
    rows = list(SwivJsonDecoder().decode({"vehicule": [_GATRA_RECORD]}, fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, SwivVehicleRow)
    assert row.feed_timestamp == ts
    assert row.vehicle_id == "1017"
    assert row.route_line_id == 16103  # raw int, no topo name lookup
    assert row.speed == 13.0  # raw, no km/h->m/s
    assert row.heading == 191.0  # localisation.cap
    assert row.destination == "Plymouth Center"
    assert row.delay_status == "on time"
    assert row.next_stop_name == "KingstonCollect"  # conduite.arretSuiv.nomCommercial
    assert row.fill_rate == 10


def test_swiv_decode_handles_lrta_shape():
    rows = list(
        SwivJsonDecoder().decode({"vehicule": [_LRTA_RECORD]}, fetched_at=1781622000)
    )
    row = rows[0]
    assert row.route_line_id == 27302
    assert row.next_stop_name == "Main&Hill"
    assert row.fill_rate is None  # LRTA omits tauxRemplissage


def test_swiv_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(SwivJsonDecoder().decode({"vehicule": [_GATRA_RECORD]}))


def test_swiv_decode_handles_null_arret_suiv():
    # A bus with no next stop sends arretSuiv: null — the prod crash.
    rec = {
        **_GATRA_RECORD,
        "conduite": {**_GATRA_RECORD["conduite"], "arretSuiv": None},
    }
    rows = list(SwivJsonDecoder().decode({"vehicule": [rec]}, fetched_at=1781622000))
    assert len(rows) == 1
    assert rows[0].next_stop_name is None
    assert rows[0].next_stop_eta is None


def test_swiv_decode_zero_fill_rate():
    rec = {**_GATRA_RECORD, "tauxRemplissage": 0}
    rows = list(SwivJsonDecoder().decode({"vehicule": [rec]}, fetched_at=1781622000))
    assert rows[0].fill_rate == 0  # not None, not a crash


class TestSwivJsonDecoderValidate:
    decoder = SwivJsonDecoder()

    def test_empty_returns_none(self):
        assert self.decoder.validate({"vehicule": []}) is None

    def test_both_shapes_validate(self):
        assert self.decoder.validate({"vehicule": [_GATRA_RECORD]}) is None
        assert self.decoder.validate({"vehicule": [_LRTA_RECORD]}) is None

    def test_drifted_record_reported(self):
        broken = {k: v for k, v in _GATRA_RECORD.items() if k != "conduite"}
        report = self.decoder.validate({"vehicule": [broken]})
        assert report is not None
        assert report.missing_required == frozenset({"conduite"})

    def test_drifted_record_skipped_in_decode(self):
        broken = {k: v for k, v in _GATRA_RECORD.items() if k != "id"}
        rows = list(
            self.decoder.decode(
                {"vehicule": [broken, _GATRA_RECORD]}, fetched_at=1781622000
            )
        )
        assert len(rows) == 1
        assert rows[0].vehicle_id == "1017"


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


# ---------------------------------------------------------------------------
# StandardDecoder – trip updates (explode + field capture)
# ---------------------------------------------------------------------------


def _make_trip_update_feed(
    feed_timestamp: int = 1_700_000_000,
    trip_update_timestamp: int = 1_700_000_030,
    trip_id: str = "trip-1",
    route_id: str = "route-A",
    direction_id: int = 0,
    start_date: str = "20240101",
    start_time: str = "08:15:00",
    vehicle_id: str = "v1",
    stop_time_updates: tuple[tuple[str, int], ...] = (
        ("stop-1", 1_700_000_100),
        ("stop-2", 1_700_000_200),
        ("stop-3", 1_700_000_300),
    ),
) -> gtfs.FeedMessage:
    """FeedMessage with a single trip_update entity containing multiple stop_time_updates."""
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = feed_timestamp

    entity = feed.entity.add()
    entity.id = "tu-1"
    tu = entity.trip_update
    tu.timestamp = trip_update_timestamp
    tu.trip.trip_id = trip_id
    tu.trip.route_id = route_id
    tu.trip.direction_id = direction_id
    tu.trip.start_date = start_date
    tu.trip.start_time = start_time
    tu.vehicle.id = vehicle_id

    for stop_id, arrival_time in stop_time_updates:
        stu = tu.stop_time_update.add()
        stu.stop_id = stop_id
        stu.arrival.time = arrival_time

    return feed


class TestStandardDecoderTripUpdates:
    decoder = StandardDecoder()

    def test_one_stop_time_update_per_row(self):
        feed = _make_trip_update_feed()
        rows = list(self.decoder.decode(feed))
        assert len(rows) == 3
        assert all(isinstance(r, StopTimeUpdateRow) for r in rows)
        assert [r.stop_id for r in rows] == ["stop-1", "stop-2", "stop-3"]

    def test_trip_level_fields_repeated_across_rows(self):
        feed = _make_trip_update_feed()
        rows = list(self.decoder.decode(feed))
        assert {r.trip_id for r in rows} == {"trip-1"}
        assert {r.route_id for r in rows} == {"route-A"}
        assert {r.start_time for r in rows} == {"08:15:00"}

    def test_trip_update_timestamp_captured(self):
        feed = _make_trip_update_feed(trip_update_timestamp=1_700_000_030)
        rows = list(self.decoder.decode(feed))
        # All rows from the same TU share its timestamp
        assert {r.trip_update_timestamp for r in rows} == {1_700_000_030}

    def test_missing_trip_update_timestamp_is_none(self):
        feed = gtfs.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 1_700_000_000
        entity = feed.entity.add()
        entity.id = "tu-min"
        tu = entity.trip_update
        tu.trip.trip_id = "trip-min"
        stu = tu.stop_time_update.add()
        stu.stop_id = "stop-min"

        rows = list(self.decoder.decode(feed))
        assert rows[0].trip_update_timestamp is None


# ---------------------------------------------------------------------------
# VTA (Vineyard Transit Authority) JSON decoder — flat array, single agency.
# Live endpoint is often empty (seasonal/off-hours), so the baseline mirrors
# nibble's adapter fixture shape.
# ---------------------------------------------------------------------------

_VTA_RECORD = {
    "vehicleId": 22,
    "name": "103",
    "patternId": 1401,
    "headsignText": "3",
    "lat": 41.455,
    "lng": -70.601,
    "velocity": 18,
    "bearing": 279,
    "lastUpdate": "2026-03-18T15:46:58",
    "vehicleStateId": 1,
    "bypassDailyTripId": None,
}


def test_vta_decode_smoke():
    ts = 1781622000
    rows = list(VtaJsonDecoder().decode([_VTA_RECORD], fetched_at=ts))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, VtaVehicleRow)
    assert row.feed_timestamp == ts
    assert row.vehicle_id == "22"  # str of int id
    assert row.headsign_text == "3"  # raw (route normalization deferred)
    assert row.velocity == 18  # raw mph, no m/s conversion
    assert row.last_update == "2026-03-18T15:46:58"  # raw naive string
    assert row.bearing == 279


def test_vta_empty_array_decodes_to_nothing():
    # The live feed is frequently an empty array; that must land cleanly.
    assert list(VtaJsonDecoder().decode([], fetched_at=1781622000)) == []


def test_vta_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(VtaJsonDecoder().decode([_VTA_RECORD]))


class TestVtaJsonDecoderValidate:
    decoder = VtaJsonDecoder()

    def test_empty_payload(self):
        assert self.decoder.validate([]) is None

    def test_valid_payload(self):
        assert self.decoder.validate([_VTA_RECORD]) is None

    def test_drifted_record_reported(self):
        broken = {k: v for k, v in _VTA_RECORD.items() if k != "vehicleId"}
        report = self.decoder.validate([broken])
        assert report is not None
        assert report.missing_required == frozenset({"vehicleId"})

    def test_drifted_record_skipped_in_decode(self):
        broken = {k: v for k, v in _VTA_RECORD.items() if k != "lat"}
        rows = list(self.decoder.decode([broken, _VTA_RECORD], fetched_at=1781622000))
        assert len(rows) == 1
        assert rows[0].vehicle_id == "22"


# ---------------------------------------------------------------------------
# Passio GO! JSON decoder — POST feed; vehicles nested under "buses" as a
# dict-of-lists with a "-1" sentinel key; numerics arrive as strings
# ---------------------------------------------------------------------------

_PASSIO_RECORD = {
    "deviceId": 422718,
    "created": "03:41 PM",
    "createdTime": "03:41 PM",
    "paxLoad": 4,
    "bus": "  32  ",
    "busId": 11253,
    "userId": "2771",
    "routeBlockId": "82704",
    "latitude": "42.588739500",  # string -> float
    "longitude": "-72.315955300",
    "calculatedCourse": "93.95947015171777",
    "tripId": "399959",
    "outOfService": 0,
    "more": "102",
    "totalCap": 22,
    "color": "#ed9119",
    "busName": "  32  ",
    "busType": "",
    "routeId": "30848",
    "route": "Rt. 32 Orange/Greenfield",
    "outdated": 0,
}


def _passio_payload(*records):
    return {
        "buses": {str(r["deviceId"]): [r] for r in records},
        "microtime": 0,
        "time": 0,
        "debug": [],
    }


def test_passio_decode_smoke():
    ts = 1781622000
    rows = list(
        PassioGoDecoder().decode(_passio_payload(_PASSIO_RECORD), fetched_at=ts)
    )
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, PassioVehicleRow)
    assert row.feed_timestamp == ts
    assert row.vehicle_id == "11253"  # str(busId)
    assert row.route_id == "30848"  # raw, no myid->name lookup
    assert row.route_name == "Rt. 32 Orange/Greenfield"  # raw human name kept too
    assert row.latitude == 42.5887395  # string -> float
    assert row.calculated_course == 93.95947015171777
    assert row.pax_load == 4


def test_passio_skips_sentinel_minus_one():
    payload = _passio_payload(_PASSIO_RECORD)
    payload["buses"]["-1"] = [
        {
            "busId": -1,
            "latitude": "0",
            "longitude": "0",
            "calculatedCourse": "0",
            "routeId": "x",
            "tripId": "x",
        }
    ]
    rows = list(PassioGoDecoder().decode(payload, fetched_at=1781622000))
    assert len(rows) == 1  # the -1 sentinel entry is skipped
    assert rows[0].vehicle_id == "11253"


def test_passio_decode_requires_fetched_at():
    with pytest.raises(ValueError):
        list(PassioGoDecoder().decode(_passio_payload(_PASSIO_RECORD)))


class TestPassioGoDecoderValidate:
    decoder = PassioGoDecoder()

    def test_empty_buses_returns_none(self):
        assert self.decoder.validate({"buses": {}}) is None

    def test_valid_payload_returns_none(self):
        assert self.decoder.validate(_passio_payload(_PASSIO_RECORD)) is None

    def test_drifted_record_reported(self):
        broken = {k: v for k, v in _PASSIO_RECORD.items() if k != "busId"}
        report = self.decoder.validate(_passio_payload({**broken, "deviceId": 1}))
        assert report is not None
        assert report.missing_required == frozenset({"busId"})


# ---------------------------------------------------------------------------
# Invariant: every decoder's TableSpec.dedup_keys must name real Row fields,
# so the rollup never dedups on a column that doesn't exist.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(Decoder._registry))
def test_dedup_keys_reference_real_row_fields(name):
    decoder = Decoder.from_name(name)
    for row_cls, spec in decoder.produces.items():
        row_fields = {f.name for f in _dc_fields(row_cls)}
        missing = set(spec.dedup_keys) - row_fields
        assert not missing, (
            f"{name}: dedup_keys {sorted(missing)} not in {row_cls.__name__} fields"
        )
