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
use arrow::array::RecordBatch;
use arrow::datatypes::Schema;

pub struct TfnswStopTimeUpdateRowBuilder {
    // TODO: mirror crate::trip_update's builder fields, plus:
    //   departure_occupancy_status: StringBuilder,   // the field-6 divergence
}

impl TfnswStopTimeUpdateRowBuilder {
    pub fn new() -> Self {
        todo!("one <Type>Builder::new() per field")
    }

    /// One row per `tu.stop_time_update`, and one consist row per carriage in
    /// each STU's `carriage_seq_predictive_occupancy`.
    ///
    /// TODO: hoist the trip-level fields (trip_id, route_id, vehicle_id, ...)
    /// out of the STU loop as `crate::trip_update` does -- they are constant
    /// across the loop and cloning them per STU is the cost that motivated the
    /// Rust port in the first place.
    ///
    /// Per STU, after appending the row:
    ///     for car in &stu.carriage_seq_predictive_occupancy {
    ///         consist.append(car, ConsistOrigin::StopTimeUpdate, header,
    ///                        vehicle_id, trip_id, stu.stop_sequence,
    ///                        stu.stop_id.as_deref())?;
    ///     }
    pub fn append(
        &mut self,
        tu: &tfnsw_realtime::TripUpdate,
        header: &tfnsw_realtime::FeedHeader,
        consist: &mut ConsistRowBuilder,
    ) -> Result<(), prost::UnknownEnumValue> {
        let _ = (tu, header, consist);
        todo!("append STU rows + fan out predictive consist")
    }

    pub fn schema() -> Schema {
        todo!("declare the Arrow schema")
    }

    pub fn finish(self) -> RecordBatch {
        todo!("build the RecordBatch")
    }
}

impl Default for TfnswStopTimeUpdateRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
