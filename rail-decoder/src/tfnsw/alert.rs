//! TfNSW Alert -> alert rows (one row per informed_entity).
//!
//! The thinnest of the four: TfNSW's Alert matches canonical on every field
//! number and enum value that matters here. The only divergences are that
//! `severity_level` has no `[default = UNKNOWN_SEVERITY]` (so absent stays
//! absent rather than defaulting), and `EntitySelector.trip` references the
//! TfNSW `TripDescriptor`, whose ScheduleRelationship keeps REPLACEMENT = 5.
//!
//! That makes this module a good candidate for the "should these be shared via
//! a trait instead of duplicated?" question -- if any of the four collapses
//! back into its canonical sibling, it is this one.

use crate::tfnsw_realtime;
use arrow::array::RecordBatch;
use arrow::datatypes::Schema;

pub struct TfnswAlertRowBuilder {
    // TODO: mirror crate::alert's builder fields.
}

impl TfnswAlertRowBuilder {
    pub fn new() -> Self {
        todo!("one <Type>Builder::new() per field")
    }

    pub fn append(
        &mut self,
        alert: &tfnsw_realtime::Alert,
        alert_id: &str,
        header: &tfnsw_realtime::FeedHeader,
    ) -> Result<(), prost::UnknownEnumValue> {
        let _ = (alert, alert_id, header);
        todo!("append one row per informed_entity")
    }

    pub fn schema() -> Schema {
        todo!("declare the Arrow schema")
    }

    pub fn finish(self) -> RecordBatch {
        todo!("build the RecordBatch")
    }
}

impl Default for TfnswAlertRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
