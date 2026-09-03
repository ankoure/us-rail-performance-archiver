//! Per-carriage rows, from both TfNSW consist streams.
//!
//! Two streams carry the same `CarriageDescriptor` payload at different grain:
//!   - `VehiclePosition.consist`                       -- observed now
//!   - `StopTimeUpdate.carriage_seq_predictive_occupancy` -- predicted at a
//!     future stop
//!
//! They land in one table with an `origin` discriminator. Observed rows leave
//! `stop_sequence`/`stop_id` null; predicted rows populate them. Measured on
//! live payloads, the two streams set *disjoint* occupancy fields (observed
//! sets `occupancy_status`, predicted sets `departure_occupancy_status` and
//! never the other), so both columns exist and neither is collapsed into the
//! other -- they are different measurements, not one column plus provenance.

use crate::tfnsw_realtime;
use arrow::array::{
    BooleanBuilder, Int32Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder,
};
use arrow::datatypes::{DataType, Field, Schema};
use std::sync::Arc;

/// Which stream a carriage row came from. Kept explicit rather than inferred
/// from which occupancy column is non-null.
#[derive(Clone, Copy)]
pub enum ConsistOrigin {
    VehiclePosition,
    StopTimeUpdate,
}

impl ConsistOrigin {
    fn as_str(self) -> &'static str {
        match self {
            ConsistOrigin::VehiclePosition => "vehicle_position",
            ConsistOrigin::StopTimeUpdate => "stop_time_update",
        }
    }
}

pub struct ConsistRowBuilder {
    feed_timestamp: UInt64Builder,
    origin: StringBuilder,
    vehicle_id: StringBuilder,
    trip_id: StringBuilder,
    // Null for ConsistOrigin::VehiclePosition -- an honestly absent dimension,
    // not missing data.
    stop_sequence: UInt32Builder,
    stop_id: StringBuilder,
    // `required` in the proto, so prost gives a bare i32 with no Option.
    // NB: Metro reports 0 for every carriage and conveys order via `name`
    // instead, so this is NOT reliable for cross-agency carriage ordering.
    position_in_consist: Int32Builder,
    name: StringBuilder,
    occupancy_status: StringBuilder,
    departure_occupancy_status: StringBuilder,
    quiet_carriage: BooleanBuilder,
    toilet: StringBuilder,
    luggage_rack: BooleanBuilder,
}

impl ConsistRowBuilder {
    pub fn new() -> Self {
        Self {
            feed_timestamp: UInt64Builder::new(),
            origin: StringBuilder::new(),
            vehicle_id: StringBuilder::new(),
            trip_id: StringBuilder::new(),
            stop_sequence: UInt32Builder::new(),
            stop_id: StringBuilder::new(),
            position_in_consist: Int32Builder::new(),
            name: StringBuilder::new(),
            occupancy_status: StringBuilder::new(),
            departure_occupancy_status: StringBuilder::new(),
            quiet_carriage: BooleanBuilder::new(),
            toilet: StringBuilder::new(),
            luggage_rack: BooleanBuilder::new(),
        }
    }

    /// Append one carriage.
    ///
    /// `stop_sequence`/`stop_id` are passed as None by the VehiclePosition
    /// caller and Some(..) by the StopTimeUpdate caller, which is why they are
    /// parameters rather than read off `car`.
    pub fn append(
        &mut self,
        car: &tfnsw_realtime::CarriageDescriptor,
        origin: ConsistOrigin,
        header: &tfnsw_realtime::FeedHeader,
        vehicle_id: Option<&str>,
        trip_id: Option<&str>,
        stop_sequence: Option<u32>,
        stop_id: Option<&str>,
    ) -> Result<(), prost::UnknownEnumValue> {
        use tfnsw_realtime::carriage_descriptor::{OccupancyStatus, ToiletStatus};

        // Every fallible decode happens before the first append. This call
        // writes exactly one row across thirteen columns, so bailing partway
        // through would leave those columns at unequal lengths with no way to
        // un-append -- the builder would be permanently desynced and the
        // failure would only surface later, in RecordBatch::try_new.
        //
        // Reading the raw Option instead of prost's accessors is also what
        // keeps absent distinct from the zero variant.
        let occupancy_status = car
            .occupancy_status
            .map(OccupancyStatus::try_from)
            .transpose()?
            .map(|v| v.as_str_name());
        let departure_occupancy_status = car
            .departure_occupancy_status
            .map(OccupancyStatus::try_from)
            .transpose()?
            .map(|v| v.as_str_name());
        let toilet = car
            .toilet
            .map(ToiletStatus::try_from)
            .transpose()?
            .map(|v| v.as_str_name());

        self.feed_timestamp
            .append_value(header.timestamp.unwrap_or_default());
        self.origin.append_value(origin.as_str());
        self.vehicle_id.append_option(vehicle_id);
        self.trip_id.append_option(trip_id);
        self.stop_sequence.append_option(stop_sequence);
        self.stop_id.append_option(stop_id);
        self.position_in_consist
            .append_value(car.position_in_consist);
        self.name.append_option(car.name.as_deref());
        self.occupancy_status.append_option(occupancy_status);
        self.departure_occupancy_status
            .append_option(departure_occupancy_status);
        self.quiet_carriage.append_option(car.quiet_carriage);
        self.toilet.append_option(toilet);
        self.luggage_rack.append_option(car.luggage_rack);

        Ok(())
    }

    pub fn schema() -> Schema {
        Schema::new(vec![
            Field::new("feed_timestamp", DataType::UInt64, false),
            Field::new("origin", DataType::Utf8, false),
            Field::new("vehicle_id", DataType::Utf8, true),
            Field::new("trip_id", DataType::Utf8, true),
            Field::new("stop_sequence", DataType::UInt32, true),
            Field::new("stop_id", DataType::Utf8, true),
            Field::new("position_in_consist", DataType::Int32, false),
            Field::new("name", DataType::Utf8, true),
            Field::new("occupancy_status", DataType::Utf8, true),
            Field::new("departure_occupancy_status", DataType::Utf8, true),
            Field::new("quiet_carriage", DataType::Boolean, true),
            Field::new("toilet", DataType::Utf8, true),
            Field::new("luggage_rack", DataType::Boolean, true),
        ])
    }

    pub fn finish(mut self) -> RecordBatch {
        RecordBatch::try_new(
            Arc::new(Self::schema()),
            vec![
                Arc::new(self.feed_timestamp.finish()),
                Arc::new(self.origin.finish()),
                Arc::new(self.vehicle_id.finish()),
                Arc::new(self.trip_id.finish()),
                Arc::new(self.stop_sequence.finish()),
                Arc::new(self.stop_id.finish()),
                Arc::new(self.position_in_consist.finish()),
                Arc::new(self.name.finish()),
                Arc::new(self.occupancy_status.finish()),
                Arc::new(self.departure_occupancy_status.finish()),
                Arc::new(self.quiet_carriage.finish()),
                Arc::new(self.toilet.finish()),
                Arc::new(self.luggage_rack.finish()),
            ],
        )
        .expect("columns match ConsistRowBuilder::schema() by construction")
    }
}

impl Default for ConsistRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
