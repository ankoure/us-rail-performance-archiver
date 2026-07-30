use crate::transit_realtime;
use arrow::array::{Float32Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder};
use arrow::datatypes::{DataType, Field, Schema};
use pyo3::prelude::*;
use std::sync::Arc;

pub struct VehicleRowBuilder {
    feed_timestamp: UInt64Builder,
    vehicle_id: StringBuilder,
    vehicle_label: StringBuilder,
    trip_id: StringBuilder,
    route_id: StringBuilder,
    direction_id: UInt32Builder,
    start_date: StringBuilder,
    start_time: StringBuilder,
    schedule_relationship: StringBuilder,
    latitude: Float32Builder,
    longitude: Float32Builder,
    bearing: Float32Builder,
    speed: Float32Builder,
    current_stop_sequence: UInt32Builder,
    stop_id: StringBuilder,
    current_status: StringBuilder,
    occupancy_status: StringBuilder,
    occupancy_percentage: UInt32Builder,
    vehicle_timestamp: UInt64Builder,
}

impl VehicleRowBuilder {
    pub fn new() -> Self {
        Self {
            feed_timestamp: UInt64Builder::new(),
            vehicle_id: StringBuilder::new(),
            vehicle_label: StringBuilder::new(),
            trip_id: StringBuilder::new(),
            route_id: StringBuilder::new(),
            direction_id: UInt32Builder::new(),
            start_date: StringBuilder::new(),
            start_time: StringBuilder::new(),
            schedule_relationship: StringBuilder::new(),
            latitude: Float32Builder::new(),
            longitude: Float32Builder::new(),
            bearing: Float32Builder::new(),
            speed: Float32Builder::new(),
            current_stop_sequence: UInt32Builder::new(),
            stop_id: StringBuilder::new(),
            current_status: StringBuilder::new(),
            occupancy_status: StringBuilder::new(),
            occupancy_percentage: UInt32Builder::new(),
            vehicle_timestamp: UInt64Builder::new(),
        }
    }
}

impl VehicleRowBuilder {
    pub fn append(
        &mut self,
        vp: &transit_realtime::VehiclePosition,
        header: &transit_realtime::FeedHeader,
    ) -> Result<(), prost::UnknownEnumValue> {
        // Shape 1: non-optional
        self.feed_timestamp
            .append_value(header.timestamp.unwrap_or(0));

        // Shape 4: nested-submessage Option<String>
        self.vehicle_id
            .append_option(vp.vehicle.as_ref().and_then(|v| v.id.clone()));
        self.vehicle_label
            .append_option(vp.vehicle.as_ref().and_then(|v| v.label.clone()));
        self.trip_id
            .append_option(vp.trip.as_ref().and_then(|t| t.trip_id.clone()));
        self.route_id
            .append_option(vp.trip.as_ref().and_then(|t| t.route_id.clone()));

        // Shape 4, numeric variant
        self.direction_id
            .append_option(vp.trip.as_ref().and_then(|t| t.direction_id));

        self.start_date
            .append_option(vp.trip.as_ref().and_then(|t| t.start_date.clone()));
        self.start_time
            .append_option(vp.trip.as_ref().and_then(|t| t.start_time.clone()));

        // Shape 7: nested enum
        self.schedule_relationship.append_option(
            vp.trip
                .as_ref()
                .and_then(|t| t.schedule_relationship)
                .map(|raw| {
                    transit_realtime::trip_descriptor::ScheduleRelationship::try_from(raw)
                        .map(|v| v.as_str_name())
                })
                .transpose()?,
        );

        // Position sub-message: latitude/longitude are required within Position,
        // bearing/speed are optional within Position, and Position itself is optional.
        self.latitude
            .append_option(vp.position.as_ref().map(|p| p.latitude));
        self.longitude
            .append_option(vp.position.as_ref().map(|p| p.longitude));
        self.bearing
            .append_option(vp.position.as_ref().and_then(|p| p.bearing));
        self.speed
            .append_option(vp.position.as_ref().and_then(|p| p.speed));

        // Shape 2/3: top-level optional scalar / String fields
        self.current_stop_sequence
            .append_option(vp.current_stop_sequence);
        self.stop_id.append_option(vp.stop_id.clone());

        // Shape 6: top-level optional enum
        self.current_status.append_option(
            vp.current_status
                .map(|raw| {
                    transit_realtime::vehicle_position::VehicleStopStatus::try_from(raw)
                        .map(|v| v.as_str_name())
                })
                .transpose()?,
        );
        self.occupancy_status.append_option(
            vp.occupancy_status
                .map(|raw| {
                    transit_realtime::vehicle_position::OccupancyStatus::try_from(raw)
                        .map(|v| v.as_str_name())
                })
                .transpose()?,
        );

        self.occupancy_percentage
            .append_option(vp.occupancy_percentage);
        self.vehicle_timestamp.append_option(vp.timestamp);
        Ok(())
    }
}

impl VehicleRowBuilder {
    pub fn finish(mut self) -> RecordBatch {
        let schema = Arc::new(Schema::new(vec![
            Field::new("feed_timestamp", DataType::UInt64, false),
            Field::new("vehicle_id", DataType::Utf8, true),
            Field::new("vehicle_label", DataType::Utf8, true),
            Field::new("trip_id", DataType::Utf8, true),
            Field::new("route_id", DataType::Utf8, true),
            Field::new("direction_id", DataType::UInt32, true),
            Field::new("start_date", DataType::Utf8, true),
            Field::new("start_time", DataType::Utf8, true),
            Field::new("schedule_relationship", DataType::Utf8, true),
            Field::new("latitude", DataType::Float32, true),
            Field::new("longitude", DataType::Float32, true),
            Field::new("bearing", DataType::Float32, true),
            Field::new("speed", DataType::Float32, true),
            Field::new("current_stop_sequence", DataType::UInt32, true),
            Field::new("stop_id", DataType::Utf8, true),
            Field::new("current_status", DataType::Utf8, true),
            Field::new("occupancy_status", DataType::Utf8, true),
            Field::new("occupancy_percentage", DataType::UInt32, true),
            Field::new("vehicle_timestamp", DataType::UInt64, true),
        ]));

        RecordBatch::try_new(
            schema,
            vec![
                Arc::new(self.feed_timestamp.finish()),
                Arc::new(self.vehicle_id.finish()),
                Arc::new(self.vehicle_label.finish()),
                Arc::new(self.trip_id.finish()),
                Arc::new(self.route_id.finish()),
                Arc::new(self.direction_id.finish()),
                Arc::new(self.start_date.finish()),
                Arc::new(self.start_time.finish()),
                Arc::new(self.schedule_relationship.finish()),
                Arc::new(self.latitude.finish()),
                Arc::new(self.longitude.finish()),
                Arc::new(self.bearing.finish()),
                Arc::new(self.speed.finish()),
                Arc::new(self.current_stop_sequence.finish()),
                Arc::new(self.stop_id.finish()),
                Arc::new(self.current_status.finish()),
                Arc::new(self.occupancy_status.finish()),
                Arc::new(self.occupancy_percentage.finish()),
                Arc::new(self.vehicle_timestamp.finish()),
            ],
        )
        .unwrap()
    }
}

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
) -> Result<VehicleRow, prost::UnknownEnumValue> {
    Ok(VehicleRow {
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
                    .map(|v| v.as_str_name())
            })
            .transpose()?,
        latitude: vp.position.as_ref().map(|p| p.latitude),
        longitude: vp.position.as_ref().map(|p| p.longitude),
        bearing: vp.position.as_ref().and_then(|p| p.bearing),
        speed: vp.position.as_ref().and_then(|p| p.speed),
        current_stop_sequence: vp.current_stop_sequence,
        stop_id: vp.stop_id.clone(),
        current_status: vp
            .current_status
            .map(|raw| {
                transit_realtime::vehicle_position::VehicleStopStatus::try_from(raw)
                    .map(|v| v.as_str_name())
            })
            .transpose()?,
        occupancy_status: vp
            .occupancy_status
            .map(|raw| {
                transit_realtime::vehicle_position::OccupancyStatus::try_from(raw)
                    .map(|v| v.as_str_name())
            })
            .transpose()?,
        occupancy_percentage: vp.occupancy_percentage,
        vehicle_timestamp: vp.timestamp,
    })
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

        assert_eq!(decode_vehicle(&vp, &header).unwrap(), expected);
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

        assert_eq!(decode_vehicle(&vp, &header).unwrap(), expected);
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

        let row = decode_vehicle(&vp, &header).unwrap();
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

        let row = decode_vehicle(&vp, &header).unwrap();

        assert_eq!(row.latitude, Some(40.7128));
        assert_eq!(row.longitude, Some(-74.0060));
        assert_eq!(row.bearing, None);
        assert_eq!(row.speed, None);
    }
}
#[cfg(test)]
mod vehicle_row_builder_tests {
    use super::*;
    use arrow::array::{Float32Array, StringArray, UInt64Array};
    use transit_realtime::{
        FeedHeader, Position, TripDescriptor, VehicleDescriptor, VehiclePosition,
    };

    #[test]
    fn builds_correct_record_batch_from_known_input() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };
        let vp = VehiclePosition {
            trip: Some(TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                ..Default::default()
            }),
            vehicle: Some(VehicleDescriptor {
                id: Some("v1".to_string()),
                ..Default::default()
            }),
            position: Some(Position {
                latitude: 40.7128,
                longitude: -74.0060,
                ..Default::default()
            }),
            ..Default::default()
        };

        let mut builder = VehicleRowBuilder::new();
        builder.append(&vp, &header).unwrap();
        let batch = builder.finish();

        assert_eq!(batch.num_rows(), 1);

        let schema = batch.schema();
        let ts_idx = schema.index_of("feed_timestamp").unwrap();
        let trip_idx = schema.index_of("trip_id").unwrap();
        let vid_idx = schema.index_of("vehicle_id").unwrap();
        let lat_idx = schema.index_of("latitude").unwrap();

        let ts_col = batch
            .column(ts_idx)
            .as_any()
            .downcast_ref::<UInt64Array>()
            .unwrap();
        let trip_col = batch
            .column(trip_idx)
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap();
        let vid_col = batch
            .column(vid_idx)
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap();
        let lat_col = batch
            .column(lat_idx)
            .as_any()
            .downcast_ref::<Float32Array>()
            .unwrap();

        assert_eq!(ts_col.value(0), 1_700_000_000);
        assert_eq!(trip_col.value(0), "trip-1");
        assert_eq!(vid_col.value(0), "v1");
        assert_eq!(lat_col.value(0), 40.7128);
    }
}
