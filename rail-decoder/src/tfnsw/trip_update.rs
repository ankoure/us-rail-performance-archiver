//! TfNSW TripUpdate -> stop-time-update rows (one row per StopTimeUpdate).
//!
//! Two TfNSW-specific things happen here, and they are the whole reason this
//! module exists separately from `crate::trip_update`:
//!
//!  1. `StopTimeUpdate.departure_occupancy_status` is field **6**. Canonical
//!     GTFS-RT uses field 6 for `stop_time_properties` (a submessage), so the
//!     canonical decoder cannot read this -- it is a wire-type mismatch and the
//!     value is dropped. Measured: populated on ~1919 of 4439 STUs on a live
//!     Sydney Trains payload, all of which are lost today.
//!
//!  2. `carriage_seq_predictive_occupancy` (tag 1007) fans out into the consist
//!     table with ConsistOrigin::StopTimeUpdate. This is the LARGER of the two
//!     carriage streams -- ~15,020 rows per payload vs ~1883 observed.

use super::consist::{ConsistOrigin, ConsistRowBuilder};
use crate::tfnsw_realtime;
use arrow::array::{
    Int32Builder, Int64Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder,
};
use arrow::datatypes::{DataType, Field, Schema};
use std::sync::Arc;

pub struct TfnswStopTimeUpdateRowBuilder {
    feed_timestamp: UInt64Builder,
    trip_update_timestamp: UInt64Builder,
    trip_id: StringBuilder,
    route_id: StringBuilder,
    direction_id: UInt32Builder,
    start_date: StringBuilder,
    start_time: StringBuilder,
    schedule_relationship: StringBuilder,
    vehicle_id: StringBuilder,
    vehicle_label: StringBuilder,
    stop_sequence: UInt32Builder,
    stop_id: StringBuilder,
    arrival_delay: Int32Builder,
    arrival_time: Int64Builder,
    arrival_uncertainty: Int32Builder,
    departure_delay: Int32Builder,
    departure_time: Int64Builder,
    departure_uncertainty: Int32Builder,
    stop_time_schedule_relationship: StringBuilder,
    // The field-6 divergence. Appended last so the leading columns stay
    // positionally identical to crate::trip_update's, which keeps a diff of the
    // two schema() bodies readable.
    departure_occupancy_status: StringBuilder,
}

impl TfnswStopTimeUpdateRowBuilder {
    pub fn new() -> Self {
        Self {
            feed_timestamp: UInt64Builder::new(),
            trip_update_timestamp: UInt64Builder::new(),
            trip_id: StringBuilder::new(),
            route_id: StringBuilder::new(),
            direction_id: UInt32Builder::new(),
            start_date: StringBuilder::new(),
            start_time: StringBuilder::new(),
            schedule_relationship: StringBuilder::new(),
            vehicle_id: StringBuilder::new(),
            vehicle_label: StringBuilder::new(),
            stop_sequence: UInt32Builder::new(),
            stop_id: StringBuilder::new(),
            arrival_delay: Int32Builder::new(),
            arrival_time: Int64Builder::new(),
            arrival_uncertainty: Int32Builder::new(),
            departure_delay: Int32Builder::new(),
            departure_time: Int64Builder::new(),
            departure_uncertainty: Int32Builder::new(),
            stop_time_schedule_relationship: StringBuilder::new(),
            departure_occupancy_status: StringBuilder::new(),
        }
    }

    /// One row per `tu.stop_time_update`, and one consist row per carriage in
    /// each STU's `carriage_seq_predictive_occupancy`.
    ///
    /// Error boundary is per-STU: a failure leaves rows for earlier STUs
    /// already appended, but every column at equal length. See the note on the
    /// consist fan-out for the one place that guarantee does not span builders.
    pub fn append(
        &mut self,
        tu: &tfnsw_realtime::TripUpdate,
        header: &tfnsw_realtime::FeedHeader,
        consist: &mut ConsistRowBuilder,
    ) -> Result<(), prost::UnknownEnumValue> {
        use tfnsw_realtime::trip_descriptor::ScheduleRelationship as TripRel;
        use tfnsw_realtime::trip_update::stop_time_update::ScheduleRelationship as StuRel;
        use tfnsw_realtime::vehicle_position::OccupancyStatus;

        // Trip-level values, computed once. Cloning these per STU is exactly
        // the cost the port exists to remove -- a Sydney Trains payload runs
        // ~4400 STUs against a few hundred trips.
        //
        // `tu.trip` is `required` in the proto, so prost gives a bare
        // TripDescriptor with no Option to unwrap.
        let feed_timestamp = header.timestamp.unwrap_or_default();
        let trip_update_timestamp = tu.timestamp;
        let trip_id = tu.trip.trip_id.as_deref();
        let route_id = tu.trip.route_id.as_deref();
        let direction_id = tu.trip.direction_id;
        let start_date = tu.trip.start_date.as_deref();
        let start_time = tu.trip.start_time.as_deref();

        // TfNSW keeps REPLACEMENT = 5, which canonical GTFS-RT dropped. On the
        // canonical path this try_from returns Err and takes the whole payload
        // with it; here it resolves. Second reason this module is separate.
        let schedule_relationship = tu
            .trip
            .schedule_relationship
            .map(TripRel::try_from)
            .transpose()?
            .map(|v| v.as_str_name());

        let vehicle = tu.vehicle.as_ref();
        let vehicle_id = vehicle.and_then(|v| v.id.as_deref());
        let vehicle_label = vehicle.and_then(|v| v.label.as_deref());

        for stu in &tu.stop_time_update {
            // Both fallible decodes hoisted above this STU's first append, so
            // an unknown value cannot desync the columns mid-row.
            let stop_time_schedule_relationship = stu
                .schedule_relationship
                .map(StuRel::try_from)
                .transpose()?
                .map(|v| v.as_str_name());
            let departure_occupancy_status = stu
                .departure_occupancy_status
                .map(OccupancyStatus::try_from)
                .transpose()?
                .map(|v| v.as_str_name());

            let arrival = stu.arrival.as_ref();
            let departure = stu.departure.as_ref();

            self.feed_timestamp.append_value(feed_timestamp);
            self.trip_update_timestamp
                .append_option(trip_update_timestamp);
            self.trip_id.append_option(trip_id);
            self.route_id.append_option(route_id);
            self.direction_id.append_option(direction_id);
            self.start_date.append_option(start_date);
            self.start_time.append_option(start_time);
            self.schedule_relationship
                .append_option(schedule_relationship);
            self.vehicle_id.append_option(vehicle_id);
            self.vehicle_label.append_option(vehicle_label);
            self.stop_sequence.append_option(stu.stop_sequence);
            self.stop_id.append_option(stu.stop_id.as_deref());
            self.arrival_delay
                .append_option(arrival.and_then(|e| e.delay));
            self.arrival_time
                .append_option(arrival.and_then(|e| e.time));
            self.arrival_uncertainty
                .append_option(arrival.and_then(|e| e.uncertainty));
            self.departure_delay
                .append_option(departure.and_then(|e| e.delay));
            self.departure_time
                .append_option(departure.and_then(|e| e.time));
            self.departure_uncertainty
                .append_option(departure.and_then(|e| e.uncertainty));
            self.stop_time_schedule_relationship
                .append_option(stop_time_schedule_relationship);
            self.departure_occupancy_status
                .append_option(departure_occupancy_status);

            // Fan out predictive occupancy. NB: this row is already appended
            // above, so a consist decode error leaves the STU row present with
            // only some of its carriages. Each builder stays internally
            // consistent; the pair does not roll back together.
            for car in &stu.carriage_seq_predictive_occupancy {
                consist.append(
                    car,
                    ConsistOrigin::StopTimeUpdate,
                    header,
                    vehicle_id,
                    trip_id,
                    stu.stop_sequence,
                    stu.stop_id.as_deref(),
                )?;
            }
        }

        Ok(())
    }

    pub fn schema() -> Schema {
        Schema::new(vec![
            Field::new("feed_timestamp", DataType::UInt64, false),
            Field::new("trip_update_timestamp", DataType::UInt64, true),
            Field::new("trip_id", DataType::Utf8, true),
            Field::new("route_id", DataType::Utf8, true),
            Field::new("direction_id", DataType::UInt32, true),
            Field::new("start_date", DataType::Utf8, true),
            Field::new("start_time", DataType::Utf8, true),
            Field::new("schedule_relationship", DataType::Utf8, true),
            Field::new("vehicle_id", DataType::Utf8, true),
            Field::new("vehicle_label", DataType::Utf8, true),
            Field::new("stop_sequence", DataType::UInt32, true),
            Field::new("stop_id", DataType::Utf8, true),
            Field::new("arrival_delay", DataType::Int32, true),
            Field::new("arrival_time", DataType::Int64, true),
            Field::new("arrival_uncertainty", DataType::Int32, true),
            Field::new("departure_delay", DataType::Int32, true),
            Field::new("departure_time", DataType::Int64, true),
            Field::new("departure_uncertainty", DataType::Int32, true),
            Field::new("stop_time_schedule_relationship", DataType::Utf8, true),
            Field::new("departure_occupancy_status", DataType::Utf8, true),
        ])
    }

    pub fn finish(mut self) -> RecordBatch {
        RecordBatch::try_new(
            Arc::new(Self::schema()),
            vec![
                Arc::new(self.feed_timestamp.finish()),
                Arc::new(self.trip_update_timestamp.finish()),
                Arc::new(self.trip_id.finish()),
                Arc::new(self.route_id.finish()),
                Arc::new(self.direction_id.finish()),
                Arc::new(self.start_date.finish()),
                Arc::new(self.start_time.finish()),
                Arc::new(self.schedule_relationship.finish()),
                Arc::new(self.vehicle_id.finish()),
                Arc::new(self.vehicle_label.finish()),
                Arc::new(self.stop_sequence.finish()),
                Arc::new(self.stop_id.finish()),
                Arc::new(self.arrival_delay.finish()),
                Arc::new(self.arrival_time.finish()),
                Arc::new(self.arrival_uncertainty.finish()),
                Arc::new(self.departure_delay.finish()),
                Arc::new(self.departure_time.finish()),
                Arc::new(self.departure_uncertainty.finish()),
                Arc::new(self.stop_time_schedule_relationship.finish()),
                Arc::new(self.departure_occupancy_status.finish()),
            ],
        )
        .expect("columns match TfnswStopTimeUpdateRowBuilder::schema() by construction")
    }
}

impl Default for TfnswStopTimeUpdateRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
