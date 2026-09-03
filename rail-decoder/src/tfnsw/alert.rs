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
use arrow::array::{Int32Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder};
use arrow::datatypes::{DataType, Field, Schema};
use std::sync::Arc;

/// First available translation's text. TfNSW feeds are single-language in
/// practice, so there is no language preference to express here; if that stops
/// being true this is the one place to add one.
fn first_translation(ts: Option<&tfnsw_realtime::TranslatedString>) -> Option<String> {
    ts.and_then(|t| t.translation.first())
        .map(|tr| tr.text.clone())
}

pub struct TfnswAlertRowBuilder {
    feed_timestamp: UInt64Builder,
    alert_id: StringBuilder,
    cause: StringBuilder,
    effect: StringBuilder,
    url: StringBuilder,
    header_text: StringBuilder,
    description_text: StringBuilder,
    agency_id: StringBuilder,
    route_id: StringBuilder,
    route_type: Int32Builder,
    direction_id: UInt32Builder,
    trip_id: StringBuilder,
    stop_id: StringBuilder,
    severity_level: StringBuilder,
}

impl TfnswAlertRowBuilder {
    pub fn new() -> Self {
        Self {
            feed_timestamp: UInt64Builder::new(),
            alert_id: StringBuilder::new(),
            cause: StringBuilder::new(),
            effect: StringBuilder::new(),
            url: StringBuilder::new(),
            header_text: StringBuilder::new(),
            description_text: StringBuilder::new(),
            agency_id: StringBuilder::new(),
            route_id: StringBuilder::new(),
            route_type: Int32Builder::new(),
            direction_id: UInt32Builder::new(),
            trip_id: StringBuilder::new(),
            stop_id: StringBuilder::new(),
            severity_level: StringBuilder::new(),
        }
    }

    /// Appends one row per `informed_entity`. An alert with an empty
    /// `informed_entity` list contributes no rows at all -- same behaviour as
    /// the canonical builder, but worth knowing it is a silent drop.
    pub fn append(
        &mut self,
        alert: &tfnsw_realtime::Alert,
        alert_id: &str,
        header: &tfnsw_realtime::FeedHeader,
    ) -> Result<(), prost::UnknownEnumValue> {
        // All fallible work is hoisted above the loop, so an unknown enum value
        // returns Err before any column is touched. That matters: a partial
        // append would leave the columns at unequal lengths and RecordBatch
        // construction would fail much later, far from the cause.
        //
        // Matching on the raw Option rather than calling prost's cause() /
        // effect() / severity_level() accessors is what preserves absent-as-
        // absent. The accessors substitute the proto default (or the zero
        // variant when there is no `[default]`), which is exactly the
        // divergence called out in the module docs.
        let cause = alert
            .cause
            .map(tfnsw_realtime::alert::Cause::try_from)
            .transpose()?
            .map(|c| c.as_str_name());
        let effect = alert
            .effect
            .map(tfnsw_realtime::alert::Effect::try_from)
            .transpose()?
            .map(|e| e.as_str_name());
        let severity_level = alert
            .severity_level
            .map(tfnsw_realtime::alert::SeverityLevel::try_from)
            .transpose()?
            .map(|s| s.as_str_name());

        // Alert-level values, computed once and repeated across every entity
        // row -- same hoisting pattern as StopTimeUpdateRowBuilder's trip-level
        // bindings.
        let feed_timestamp = header.timestamp.unwrap_or_default();
        let url = first_translation(alert.url.as_ref());
        let header_text = first_translation(alert.header_text.as_ref());
        let description_text = first_translation(alert.description_text.as_ref());

        for entity in &alert.informed_entity {
            self.feed_timestamp.append_value(feed_timestamp);
            self.alert_id.append_value(alert_id);
            self.cause.append_option(cause);
            self.effect.append_option(effect);
            self.url.append_option(url.as_deref());
            self.header_text.append_option(header_text.as_deref());
            self.description_text
                .append_option(description_text.as_deref());
            self.agency_id.append_option(entity.agency_id.clone());
            self.route_id.append_option(entity.route_id.clone());
            self.route_type.append_option(entity.route_type);
            self.direction_id.append_option(entity.direction_id);
            self.trip_id
                .append_option(entity.trip.as_ref().and_then(|t| t.trip_id.clone()));
            self.stop_id.append_option(entity.stop_id.clone());
            self.severity_level.append_option(severity_level);
        }

        Ok(())
    }

    pub fn schema() -> Schema {
        Schema::new(vec![
            Field::new("feed_timestamp", DataType::UInt64, false),
            Field::new("alert_id", DataType::Utf8, false),
            Field::new("cause", DataType::Utf8, true),
            Field::new("effect", DataType::Utf8, true),
            Field::new("url", DataType::Utf8, true),
            Field::new("header_text", DataType::Utf8, true),
            Field::new("description_text", DataType::Utf8, true),
            Field::new("agency_id", DataType::Utf8, true),
            Field::new("route_id", DataType::Utf8, true),
            Field::new("route_type", DataType::Int32, true),
            Field::new("direction_id", DataType::UInt32, true),
            Field::new("trip_id", DataType::Utf8, true),
            Field::new("stop_id", DataType::Utf8, true),
            Field::new("severity_level", DataType::Utf8, true),
        ])
    }

    pub fn finish(mut self) -> RecordBatch {
        RecordBatch::try_new(
            Arc::new(Self::schema()),
            vec![
                Arc::new(self.feed_timestamp.finish()),
                Arc::new(self.alert_id.finish()),
                Arc::new(self.cause.finish()),
                Arc::new(self.effect.finish()),
                Arc::new(self.url.finish()),
                Arc::new(self.header_text.finish()),
                Arc::new(self.description_text.finish()),
                Arc::new(self.agency_id.finish()),
                Arc::new(self.route_id.finish()),
                Arc::new(self.route_type.finish()),
                Arc::new(self.direction_id.finish()),
                Arc::new(self.trip_id.finish()),
                Arc::new(self.stop_id.finish()),
                Arc::new(self.severity_level.finish()),
            ],
        )
        .expect("columns match TfnswAlertRowBuilder::schema() by construction")
    }
}

impl Default for TfnswAlertRowBuilder {
    fn default() -> Self {
        Self::new()
    }
}
