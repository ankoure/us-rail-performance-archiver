use crate::transit_realtime::{self};
use arrow::array::{Int32Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder};
use arrow::datatypes::{DataType, Field, Schema};
use pyo3::prelude::*;
use std::sync::Arc;

pub struct AlertRowBuilder {
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

impl AlertRowBuilder {
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

    pub fn append(
        &mut self,
        alert: &transit_realtime::Alert,
        alert_id: &str,
        header: &transit_realtime::FeedHeader,
    ) -> Result<(), prost::UnknownEnumValue> {
        // Alert-level values, computed once — same expressions as decode_alert's
        // hoisted `let` bindings.
        let feed_timestamp = header.timestamp.unwrap_or(0);

        let cause = alert
            .cause
            .map(|raw| transit_realtime::alert::Cause::try_from(raw).map(|v| v.as_str_name()))
            .transpose()?;
        let effect = alert
            .effect
            .map(|raw| transit_realtime::alert::Effect::try_from(raw).map(|v| v.as_str_name()))
            .transpose()?;
        let severity_level = alert
            .severity_level
            .map(|raw| {
                transit_realtime::alert::SeverityLevel::try_from(raw).map(|v| v.as_str_name())
            })
            .transpose()?;

        let header_text = alert
            .header_text
            .as_ref()
            .and_then(|ts| translated_string(ts, "en"));
        let description_text = alert
            .description_text
            .as_ref()
            .and_then(|ts| translated_string(ts, "en"));
        let url = alert
            .url
            .as_ref()
            .and_then(|ts| translated_string(ts, "en"));

        for entity in &alert.informed_entity {
            // Alert-level fields, repeated across every exploded row — each
            // .clone()'d in since they're reused across the loop.
            self.feed_timestamp.append_value(feed_timestamp);
            self.alert_id.append_value(alert_id);
            self.cause.append_option(cause);
            self.effect.append_option(effect);
            self.severity_level.append_option(severity_level);
            self.header_text.append_option(header_text.clone());
            self.description_text
                .append_option(description_text.clone());
            self.url.append_option(url.clone());

            // Per-entity fields, fresh per row — same expressions as
            // decode_alert's per-entity logic.
            self.agency_id.append_option(entity.agency_id.clone());
            self.route_id.append_option(entity.route_id.clone());
            self.route_type.append_option(entity.route_type);
            self.direction_id.append_option(entity.direction_id);
            self.trip_id
                .append_option(entity.trip.as_ref().and_then(|t| t.trip_id.clone()));
            self.stop_id.append_option(entity.stop_id.clone());
        }
        Ok(())
    }

    pub fn finish(mut self) -> RecordBatch {
        let schema = Arc::new(Schema::new(vec![
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
        ]));

        RecordBatch::try_new(
            schema,
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
        .unwrap()
    }
}

#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone, PartialEq)]
pub struct AlertRow {
    #[pyo3(get)]
    pub feed_timestamp: u64,
    #[pyo3(get)]
    pub alert_id: String,
    #[pyo3(get)]
    pub cause: Option<&'static str>,
    #[pyo3(get)]
    pub effect: Option<&'static str>,
    #[pyo3(get)]
    pub url: Option<String>,
    #[pyo3(get)]
    pub header_text: Option<String>,
    #[pyo3(get)]
    pub description_text: Option<String>,
    #[pyo3(get)]
    pub agency_id: Option<String>,
    #[pyo3(get)]
    pub route_id: Option<String>,
    #[pyo3(get)]
    pub route_type: Option<i32>,
    #[pyo3(get)]
    pub direction_id: Option<u32>,
    #[pyo3(get)]
    pub trip_id: Option<String>,
    #[pyo3(get)]
    pub stop_id: Option<String>,
    #[pyo3(get)]
    pub severity_level: Option<&'static str>,
}
fn translated_string(ts: &transit_realtime::TranslatedString, language: &str) -> Option<String> {
    if ts.translation.is_empty() {
        return None;
    }
    for t in &ts.translation {
        if t.language.as_deref() == Some(language) {
            return Some(t.text.clone());
        }
    }
    Some(ts.translation[0].text.clone())
}

pub fn decode_alert(
    alert: &transit_realtime::Alert,
    alert_id: &str,
    header: &transit_realtime::FeedHeader,
) -> Result<Vec<AlertRow>, prost::UnknownEnumValue> {
    let feed_timestamp = header.timestamp.unwrap_or(0);

    let cause = alert
        .cause
        .map(|raw| transit_realtime::alert::Cause::try_from(raw).map(|v| v.as_str_name()))
        .transpose()?;
    let effect = alert
        .effect
        .map(|raw| transit_realtime::alert::Effect::try_from(raw).map(|v| v.as_str_name()))
        .transpose()?;
    let severity_level = alert
        .severity_level
        .map(|raw| transit_realtime::alert::SeverityLevel::try_from(raw).map(|v| v.as_str_name()))
        .transpose()?;

    let header_text = alert
        .header_text
        .as_ref()
        .and_then(|ts| translated_string(ts, "en"));
    let description_text = alert
        .description_text
        .as_ref()
        .and_then(|ts| translated_string(ts, "en"));
    let url = alert
        .url
        .as_ref()
        .and_then(|ts| translated_string(ts, "en"));

    let mut rows = Vec::new();
    for entity in &alert.informed_entity {
        rows.push(AlertRow {
            feed_timestamp,
            alert_id: alert_id.to_string(),
            cause,
            effect,
            severity_level,
            header_text: header_text.clone(),
            description_text: description_text.clone(),
            url: url.clone(),
            agency_id: entity.agency_id.clone(),
            route_id: entity.route_id.clone(),
            route_type: entity.route_type,
            direction_id: entity.direction_id,
            trip_id: entity.trip.as_ref().and_then(|t| t.trip_id.clone()),
            stop_id: entity.stop_id.clone(),
        });
    }
    Ok(rows)
}

#[cfg(test)]
mod decode_alert_tests {
    use super::*;
    use transit_realtime::{
        Alert, EntitySelector, FeedHeader, TranslatedString, TripDescriptor,
        alert::{Cause, Effect, SeverityLevel},
        translated_string::Translation,
    };

    fn en_only(text: &str) -> TranslatedString {
        TranslatedString {
            translation: vec![Translation {
                text: text.to_string(),
                language: Some("en".to_string()),
            }],
        }
    }

    /// One alert affecting two routes explodes into two AlertRows, with
    /// alert-level fields (cause/effect/severity/text) repeated identically
    /// across both, and per-entity fields (route_id, trip_id, stop_id)
    /// varying per row.
    #[test]
    fn explodes_one_row_per_informed_entity() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let alert = Alert {
            cause: Some(Cause::Accident as i32),
            effect: Some(Effect::SignificantDelays as i32),
            severity_level: Some(SeverityLevel::Warning as i32),
            header_text: Some(en_only("Delays on the Red Line")),
            description_text: Some(en_only("Signal problem near Central")),
            informed_entity: vec![
                EntitySelector {
                    route_id: Some("route-A".to_string()),
                    stop_id: Some("stop-1".to_string()),
                    trip: Some(TripDescriptor {
                        trip_id: Some("trip-1".to_string()),
                        ..Default::default()
                    }),
                    ..Default::default()
                },
                EntitySelector {
                    route_id: Some("route-B".to_string()),
                    stop_id: Some("stop-2".to_string()),
                    ..Default::default()
                },
            ],
            ..Default::default()
        };

        let rows = decode_alert(&alert, "alert-1", &header).unwrap();

        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|r| r.alert_id == "alert-1"));
        assert!(rows.iter().all(|r| r.cause == Some("ACCIDENT")));
        assert!(rows.iter().all(|r| r.effect == Some("SIGNIFICANT_DELAYS")));
        assert!(rows.iter().all(|r| r.severity_level == Some("WARNING")));
        assert!(
            rows.iter()
                .all(|r| r.header_text.as_deref() == Some("Delays on the Red Line"))
        );

        assert_eq!(rows[0].route_id.as_deref(), Some("route-A"));
        assert_eq!(rows[0].trip_id.as_deref(), Some("trip-1"));
        assert_eq!(rows[1].route_id.as_deref(), Some("route-B"));
        assert_eq!(rows[1].trip_id, None);
    }

    /// An alert with no informed_entity yields no rows at all — the alert's
    /// cause/effect/text are computed but never surface anywhere.
    #[test]
    fn no_informed_entities_yields_no_rows() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };
        let alert = Alert {
            cause: Some(Cause::Weather as i32),
            informed_entity: vec![],
            ..Default::default()
        };

        assert!(decode_alert(&alert, "alert-empty", &header).unwrap().is_empty());
    }

    /// Translated-string fallback, branch 1: an "en" translation is present
    /// among several, so it should be picked over the others regardless of
    /// order.
    #[test]
    fn translated_string_prefers_english_when_present() {
        let header = FeedHeader {
            timestamp: Some(1),
            ..Default::default()
        };
        let alert = Alert {
            header_text: Some(TranslatedString {
                translation: vec![
                    Translation {
                        text: "Retard sur la ligne rouge".to_string(),
                        language: Some("fr".to_string()),
                    },
                    Translation {
                        text: "Delay on the red line".to_string(),
                        language: Some("en".to_string()),
                    },
                ],
            }),
            informed_entity: vec![EntitySelector::default()],
            ..Default::default()
        };

        let rows = decode_alert(&alert, "alert-i18n", &header).unwrap();
        assert_eq!(
            rows[0].header_text.as_deref(),
            Some("Delay on the red line")
        );
    }

    /// Translated-string fallback, branch 2: no "en" translation exists, so
    /// the first available translation is used instead of returning None.
    #[test]
    fn translated_string_falls_back_to_first_when_no_english() {
        let header = FeedHeader {
            timestamp: Some(1),
            ..Default::default()
        };
        let alert = Alert {
            description_text: Some(TranslatedString {
                translation: vec![Translation {
                    text: "Retard sur la ligne rouge".to_string(),
                    language: Some("fr".to_string()),
                }],
            }),
            informed_entity: vec![EntitySelector::default()],
            ..Default::default()
        };

        let rows = decode_alert(&alert, "alert-fr-only", &header).unwrap();
        assert_eq!(
            rows[0].description_text.as_deref(),
            Some("Retard sur la ligne rouge")
        );
    }

    /// Translated-string fallback, branch 3: an entirely empty TranslatedString
    /// (no translations at all) returns None rather than panicking on index 0.
    #[test]
    fn translated_string_empty_translation_list_is_none() {
        let header = FeedHeader {
            timestamp: Some(1),
            ..Default::default()
        };
        let alert = Alert {
            url: Some(TranslatedString {
                translation: vec![],
            }),
            informed_entity: vec![EntitySelector::default()],
            ..Default::default()
        };

        let rows = decode_alert(&alert, "alert-no-url", &header).unwrap();
        assert_eq!(rows[0].url, None);
    }
}
