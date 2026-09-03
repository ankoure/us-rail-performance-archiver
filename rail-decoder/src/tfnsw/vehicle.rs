//! TfNSW VehiclePosition -> vehicle rows.
//!
//! Deliberately a sibling of `crate::vehicle`, not a reuse of it:
//! `tfnsw_realtime::VehiclePosition` and `transit_realtime::VehiclePosition`
//! are distinct types (that is what the package rename bought), and the
//! messages genuinely differ -- no `occupancy_percentage`, no
//! `multi_carriage_details`, and a shorter `OccupancyStatus` enum.
//!
//! Emits into TWO builders: the vehicle row itself, and one consist row per
//! carriage in `vp.consist` (observed stream).

use super::consist::{ConsistOrigin, ConsistRowBuilder};
use crate::tfnsw_realtime;
use arrow::array::{
    BooleanBuilder, Float32Builder, Int32Builder, RecordBatch, StringBuilder, UInt32Builder,
    UInt64Builder,
};
use arrow::datatypes::{DataType, Field, Schema};
use std::sync::Arc;

pub struct TfnswVehicleRowBuilder {
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
    vehicle_timestamp: UInt64Builder,
    // TfNSW-only, from VehicleDescriptor.tfnsw_vehicle_descriptor (tag 1007).
    // Populated on every vehicle in every sampled feed, so worth carrying.
    vehicle_model: StringBuilder,
    // Changed from StringBuilder per the skeleton's open question: this is a
    // proto bool, and Option<bool> already carries the absent/true/false
    // tri-state that Utf8 would have encoded as a string. Matches
    // quiet_carriage/luggage_rack in consist.rs. If it turns out to be an enum
    // with more than three states, revert to Utf8 + as_str_name().
    air_conditioned: BooleanBuilder,
    wheelchair_accessible: Int32Builder,
    // TfNSW-only, from Position.track_direction (tag 1007). UP / DOWN.
    track_direction: StringBuilder,
}

impl TfnswVehicleRowBuilder {
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
            vehicle_timestamp: UInt64Builder::new(),
            vehicle_model: StringBuilder::new(),
            air_conditioned: BooleanBuilder::new(),
            wheelchair_accessible: Int32Builder::new(),
            track_direction: StringBuilder::new(),
        }
    }

    /// Append the vehicle row, then fan out `vp.consist` into `consist`.
    ///
    /// This is the observed carriage stream. It is the smaller of the two
    /// (~1883 rows per payload against ~15,020 predictive), and unlike the
    /// StopTimeUpdate path it has no stop grain to pass down -- hence the two
    /// Nones.
    pub fn append(
        &mut self,
        vp: &tfnsw_realtime::VehiclePosition,
        header: &tfnsw_realtime::FeedHeader,
        consist: &mut ConsistRowBuilder,
    ) -> Result<(), prost::UnknownEnumValue> {
        use tfnsw_realtime::TrackDirection;
        use tfnsw_realtime::trip_descriptor::ScheduleRelationship;
        use tfnsw_realtime::vehicle_position::{OccupancyStatus, VehicleStopStatus};

        let trip = vp.trip.as_ref();
        let vehicle = vp.vehicle.as_ref();
        let position = vp.position.as_ref();
        let tfnsw = vehicle.and_then(|v| v.tfnsw_vehicle_descriptor.as_ref());

        // All four enum decodes above the first append, so an unknown value
        // cannot leave the columns at unequal lengths. Same reason the raw
        // Option is read instead of prost's accessors: absent stays absent.
        let schedule_relationship = trip
            .and_then(|t| t.schedule_relationship)
            .map(ScheduleRelationship::try_from)
            .transpose()?
            .map(|v| v.as_str_name());
        let current_status = vp
            .current_status
            .map(VehicleStopStatus::try_from)
            .transpose()?
            .map(|v| v.as_str_name());
        let occupancy_status = vp
            .occupancy_status
            .map(OccupancyStatus::try_from)
            .transpose()?
            .map(|v| v.as_str_name());
        let track_direction = position
            .and_then(|p| p.track_direction)
            .map(TrackDirection::try_from)
            .transpose()?
            .map(|v| v.as_str_name());

        let vehicle_id = vehicle.and_then(|v| v.id.as_deref());
        let trip_id = trip.and_then(|t| t.trip_id.as_deref());

        self.feed_timestamp
            .append_value(header.timestamp.unwrap_or_default());
        self.vehicle_id.append_option(vehicle_id);
        self.vehicle_label
            .append_option(vehicle.and_then(|v| v.label.as_deref()));
        self.trip_id.append_option(trip_id);
        self.route_id
            .append_option(trip.and_then(|t| t.route_id.as_deref()));
        self.direction_id
            .append_option(trip.and_then(|t| t.direction_id));
        self.start_date
            .append_option(trip.and_then(|t| t.start_date.as_deref()));
        self.start_time
            .append_option(trip.and_then(|t| t.start_time.as_deref()));
        self.schedule_relationship
            .append_option(schedule_relationship);
        // latitude/longitude are `required` inside Position, so they are bare
        // f32s -- the nullability here comes from Position itself being absent.
        self.latitude.append_option(position.map(|p| p.latitude));
        self.longitude.append_option(position.map(|p| p.longitude));
        self.bearing.append_option(position.and_then(|p| p.bearing));
        self.speed.append_option(position.and_then(|p| p.speed));
        self.current_stop_sequence
            .append_option(vp.current_stop_sequence);
        self.stop_id.append_option(vp.stop_id.as_deref());
        self.current_status.append_option(current_status);
        self.occupancy_status.append_option(occupancy_status);
        self.vehicle_timestamp.append_option(vp.timestamp);
        self.vehicle_model
            .append_option(tfnsw.and_then(|t| t.vehicle_model.as_deref()));
        self.air_conditioned
            .append_option(tfnsw.and_then(|t| t.air_conditioned));
        self.wheelchair_accessible
            .append_option(tfnsw.and_then(|t| t.wheelchair_accessible));
        self.track_direction.append_option(track_direction);

        // Observed carriages. No stop grain applies to a position observation,
        // so stop_sequence/stop_id go down as None -- the absence is the point,
        // not missing data.
        for car in &vp.consist {
            consist.append(
                car,
                ConsistOrigin::VehiclePosition,
                header,
                vehicle_id,
                trip_id,
                None,
                None,
            )?;
        }

        Ok(())
    }

    pub fn schema() -> Schema {
        // Order and types must match the Python dataclass --
        // archiver/rollup.py::_schema_for_spec derives the parquet schema from
        // the dataclass, and _batch_to_parquet_table reconciles this against it.
        Schema::new(vec![
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
            Field::new("vehicle_timestamp", DataType::UInt64, true),
            Field::new("vehicle_model", DataType::Utf8, true),
            Field::new("air_conditioned", DataType::Boolean, true),
            Field::new("wheelchair_accessible", DataType::Int32, true),
            Field::new("track_direction", DataType::Utf8, true),
        ])
    }

    pub fn finish(mut self) -> RecordBatch {
        RecordBatch::try_new(
            Arc::new(Self::schema()),
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
                Arc::new(self.vehicle_timestamp.finish()),
                Arc::new(self.vehicle_model.finish()),
                Arc::new(self.air_conditioned.finish()),
                Arc::new(self.wheelchair_accessible.finish()),
                Arc::new(self.track_direction.finish()),
            ],
        )
        .expect("columns match TfnswVehicleRowBuilder::schema() by construction")
    }
}

impl Default for TfnswVehicleRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
