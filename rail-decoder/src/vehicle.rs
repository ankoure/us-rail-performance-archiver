use crate::transit_realtime;
use pyo3::prelude::*;

#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone, PartialEq)]
pub struct VehicleRow {
    #[pyo3(get)]
    pub feed_timestamp: u64,
    #[pyo3(get)]
    pub vehicle_id: Option<String>,
    #[pyo3(get)]
    pub vehicle_label: Option<String>,
    #[pyo3(get)]
    pub trip_id: Option<String>,
    #[pyo3(get)]
    pub route_id: Option<String>,
    #[pyo3(get)]
    pub direction_id: Option<u32>,
    #[pyo3(get)]
    pub start_date: Option<String>,
    #[pyo3(get)]
    pub start_time: Option<String>,
    #[pyo3(get)]
    pub schedule_relationship: Option<&'static str>,
    #[pyo3(get)]
    pub latitude: Option<f32>,
    #[pyo3(get)]
    pub longitude: Option<f32>,
    #[pyo3(get)]
    pub bearing: Option<f32>,
    #[pyo3(get)]
    pub speed: Option<f32>,
    #[pyo3(get)]
    pub current_stop_sequence: Option<u32>,
    #[pyo3(get)]
    pub stop_id: Option<String>,
    #[pyo3(get)]
    pub current_status: Option<&'static str>,
    #[pyo3(get)]
    pub occupancy_status: Option<&'static str>,
    #[pyo3(get)]
    pub occupancy_percentage: Option<u32>,
    #[pyo3(get)]
    pub vehicle_timestamp: Option<u64>,
}

pub fn decode_vehicle(
    vp: &transit_realtime::VehiclePosition,
    header: &transit_realtime::FeedHeader,
) -> VehicleRow {
    VehicleRow {
        feed_timestamp: header.timestamp.unwrap_or(0),
        vehicle_id: vp.vehicle.as_ref().and_then(|v| v.id.clone()),
        vehicle_label: vp.vehicle.as_ref().and_then(|v| v.label.clone()),
        trip_id: vp.trip.as_ref().and_then(|t| t.trip_id.clone()),
        route_id: vp.trip.as_ref().and_then(|t| t.route_id.clone()),
        direction_id: vp.trip.as_ref().and_then(|t| t.direction_id),
        start_date: vp.trip.as_ref().and_then(|t| t.start_date.clone()),
        start_time: vp.trip.as_ref().and_then(|t| t.start_time.clone()),
        schedule_relationship: vp
            .trip
            .as_ref()
            .and_then(|t| t.schedule_relationship)
            .map(|raw| {
                transit_realtime::trip_descriptor::ScheduleRelationship::try_from(raw)
                    .unwrap()
                    .as_str_name()
            }),
        latitude: vp.position.as_ref().map(|p| p.latitude),
        longitude: vp.position.as_ref().map(|p| p.longitude),
        bearing: vp.position.as_ref().and_then(|p| p.bearing),
        speed: vp.position.as_ref().and_then(|p| p.speed),
        current_stop_sequence: vp.current_stop_sequence,
        stop_id: vp.stop_id.clone(),
        current_status: vp.current_status.map(|raw| {
            transit_realtime::vehicle_position::VehicleStopStatus::try_from(raw)
                .unwrap()
                .as_str_name()
        }),
        occupancy_status: vp.occupancy_status.map(|raw| {
            transit_realtime::vehicle_position::OccupancyStatus::try_from(raw)
                .unwrap()
                .as_str_name()
        }),
        occupancy_percentage: vp.occupancy_percentage,
        vehicle_timestamp: vp.timestamp,
    }
}

#[cfg(test)]
mod decode_vehicle_tests {
    use super::*;
    use transit_realtime::{
        FeedHeader, Position, TripDescriptor, VehicleDescriptor, VehiclePosition,
        trip_descriptor::ScheduleRelationship,
        vehicle_position::{OccupancyStatus, VehicleStopStatus},
    };

    /// Mirrors Python's `make_feed()` defaults exactly (test_decoder.py),
    /// so this is checked against the same ground truth as
    /// TestStandardDecoderFullEntity.
    #[test]
    fn decodes_fully_populated_vehicle_position() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let vp = VehiclePosition {
            trip: Some(TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                route_id: Some("route-A".to_string()),
                direction_id: Some(0),
                start_date: Some("20240101".to_string()),
                start_time: Some("08:15:00".to_string()),
                schedule_relationship: Some(ScheduleRelationship::Scheduled as i32), // = 0
                ..Default::default()
            }),
            vehicle: Some(VehicleDescriptor {
                id: Some("v1".to_string()),
                label: Some("Bus 42".to_string()),
                ..Default::default()
            }),
            position: Some(Position {
                latitude: 40.7128,
                longitude: -74.0060,
                bearing: Some(90.0),
                speed: Some(12.5),
                ..Default::default()
            }),
            current_stop_sequence: Some(3),
            stop_id: Some("stop-99".to_string()),
            current_status: Some(VehicleStopStatus::InTransitTo as i32), // = 2
            timestamp: Some(1_700_000_050),
            occupancy_status: Some(OccupancyStatus::FewSeatsAvailable as i32), // = 2
            occupancy_percentage: Some(65),
            ..Default::default()
        };

        let expected = VehicleRow {
            feed_timestamp: 1_700_000_000,
            vehicle_id: Some("v1".to_string()),
            vehicle_label: Some("Bus 42".to_string()),
            trip_id: Some("trip-1".to_string()),
            route_id: Some("route-A".to_string()),
            direction_id: Some(0),
            start_date: Some("20240101".to_string()),
            start_time: Some("08:15:00".to_string()),
            schedule_relationship: Some("SCHEDULED"),
            latitude: Some(40.7128),
            longitude: Some(-74.0060),
            bearing: Some(90.0),
            speed: Some(12.5),
            current_stop_sequence: Some(3),
            stop_id: Some("stop-99".to_string()),
            current_status: Some("IN_TRANSIT_TO"),
            occupancy_status: Some("FEW_SEATS_AVAILABLE"),
            occupancy_percentage: Some(65),
            vehicle_timestamp: Some(1_700_000_050),
        };

        assert_eq!(decode_vehicle(&vp, &header), expected);
    }

    /// Mirrors Python's `make_minimal_feed()`: header.timestamp IS set,
    /// but the vehicle sub-message is otherwise empty (matches
    /// TestStandardDecoderOptionalFields, which never unsets the header).
    #[test]
    fn decodes_vehicle_position_with_optional_fields_absent() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let vp = VehiclePosition {
            trip: None,
            vehicle: None,
            position: None,
            current_stop_sequence: None,
            stop_id: None,
            current_status: None,
            timestamp: None,
            occupancy_status: None,
            occupancy_percentage: None,
            ..Default::default()
        };

        let expected = VehicleRow {
            feed_timestamp: 1_700_000_000,
            vehicle_id: None,
            vehicle_label: None,
            trip_id: None,
            route_id: None,
            direction_id: None,
            start_date: None,
            start_time: None,
            schedule_relationship: None,
            latitude: None,
            longitude: None,
            bearing: None,
            speed: None,
            current_stop_sequence: None,
            stop_id: None,
            current_status: None,
            occupancy_status: None,
            occupancy_percentage: None,
            vehicle_timestamp: None,
        };

        assert_eq!(decode_vehicle(&vp, &header), expected);
    }

    /// No Python test covers a missing feed header timestamp — `make_feed`
    /// and `make_minimal_feed` both always set it. This locks in the
    /// `unwrap_or(0)` branch on the Rust side since it's otherwise untested
    /// ground shared with the Python port.
    #[test]
    fn missing_header_timestamp_defaults_to_zero() {
        let header = FeedHeader {
            timestamp: None,
            ..Default::default()
        };
        let vp = VehiclePosition::default();

        let row = decode_vehicle(&vp, &header);
        assert_eq!(row.feed_timestamp, 0);
    }

    /// No Python test sets `position` with only latitude/longitude and no
    /// bearing/speed — `make_feed` sets all four, `make_minimal_feed` sets
    /// none. This is the one test proving the required-vs-optional split
    /// inside `Position` is handled correctly.
    #[test]
    fn splits_position_required_vs_optional_fields_correctly() {
        let header = FeedHeader {
            timestamp: Some(1),
            ..Default::default()
        };

        let vp = VehiclePosition {
            position: Some(Position {
                latitude: 40.7128,
                longitude: -74.0060,
                bearing: None,
                speed: None,
                ..Default::default()
            }),
            ..Default::default()
        };

        let row = decode_vehicle(&vp, &header);

        assert_eq!(row.latitude, Some(40.7128));
        assert_eq!(row.longitude, Some(-74.0060));
        assert_eq!(row.bearing, None);
        assert_eq!(row.speed, None);
    }
}
