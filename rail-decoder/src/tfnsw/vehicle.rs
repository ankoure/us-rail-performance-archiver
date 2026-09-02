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
use arrow::array::{Float32Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder};
use arrow::datatypes::Schema;

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
    air_conditioned: StringBuilder,
    wheelchair_accessible: UInt32Builder,
    // TfNSW-only, from Position.track_direction (tag 1007). UP / DOWN.
    track_direction: StringBuilder,
}

impl TfnswVehicleRowBuilder {
    pub fn new() -> Self {
        todo!("one <Type>Builder::new() per field, as in crate::vehicle")
    }

    /// Append the vehicle row, then fan out `vp.consist` into `consist`.
    ///
    /// TODO: body is shaped like `crate::vehicle::VehicleRowBuilder::append`
    /// for the standard columns. The TfNSW-only ones hang off
    /// `vp.vehicle.as_ref().and_then(|v| v.tfnsw_vehicle_descriptor.as_ref())`
    /// and `vp.position.as_ref().and_then(|p| p.track_direction)`.
    ///
    /// Then, for each carriage:
    ///     for car in &vp.consist {
    ///         consist.append(car, ConsistOrigin::VehiclePosition, header,
    ///                        vehicle_id, trip_id, None, None)?;
    ///     }
    /// Note `quiet_carriage`/`luggage_rack` are bools but `air_conditioned` is
    /// modelled as Utf8 above -- decide whether you want that as Boolean and
    /// make the dataclass agree.
    pub fn append(
        &mut self,
        vp: &tfnsw_realtime::VehiclePosition,
        header: &tfnsw_realtime::FeedHeader,
        consist: &mut ConsistRowBuilder,
    ) -> Result<(), prost::UnknownEnumValue> {
        let _ = (vp, header, consist);
        todo!("append vehicle row + fan out consist")
    }

    pub fn schema() -> Schema {
        // TODO: must match the Python dataclass field order/types --
        // archiver/rollup.py::_schema_for_spec derives the parquet schema from
        // the dataclass, and _batch_to_parquet_table reconciles this against it.
        todo!("declare the Arrow schema")
    }

    pub fn finish(self) -> RecordBatch {
        todo!("build the RecordBatch")
    }
}

impl Default for TfnswVehicleRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
