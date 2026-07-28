use pyo3::prelude::*;
pub mod transit_realtime {
    include!(concat!(env!("OUT_DIR"), "/transit_realtime.rs"));
}

#[derive(Debug, Clone, PartialEq)]
pub struct VehicleRow {
    pub feed_timestamp: u64,
    pub vehicle_id: Option<String>,
    pub vehicle_label: Option<String>,
    pub trip_id: Option<String>,
    pub route_id: Option<String>,
    pub direction_id: Option<u32>,
    pub start_date: Option<String>,
    pub start_time: Option<String>,
    pub schedule_relationship: Option<&'static str>,
    pub latitude: Option<f32>,
    pub longitude: Option<f32>,
    pub bearing: Option<f32>,
    pub speed: Option<f32>,
    pub current_stop_sequence: Option<u32>,
    pub stop_id: Option<String>,
    pub current_status: Option<&'static str>,
    pub occupancy_status: Option<&'static str>,
    pub occupancy_percentage: Option<u32>,
    pub vehicle_timestamp: Option<u64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StopTimeUpdateRow {
    pub feed_timestamp: u64,
    pub trip_update_timestamp: Option<u64>,
    pub trip_id: Option<String>,
    pub route_id: Option<String>,
    pub direction_id: Option<u32>,
    pub start_date: Option<String>,
    pub start_time: Option<String>,
    pub schedule_relationship: Option<&'static str>,
    pub vehicle_id: Option<String>,
    pub vehicle_label: Option<String>,
    pub stop_sequence: Option<u32>,
    pub stop_id: Option<String>,
    pub arrival_delay: Option<i32>,
    pub arrival_time: Option<i64>,
    pub arrival_uncertainty: Option<i32>,
    pub departure_delay: Option<i32>,
    pub departure_time: Option<i64>,
    pub departure_uncertainty: Option<i32>,
    pub stop_time_schedule_relationship: Option<&'static str>,
}
#[derive(Debug, Clone, PartialEq)]
pub struct AlertRow {
    pub feed_timestamp: u64,
    pub alert_id: String,
    pub cause: Option<&'static str>,
    pub effect: Option<&'static str>,
    pub url: Option<String>,
    pub header_text: Option<String>,
    pub description_text: Option<String>,
    pub agency_id: Option<String>,
    pub route_id: Option<String>,
    pub route_type: Option<i32>,
    pub direction_id: Option<u32>,
    pub trip_id: Option<String>,
    pub stop_id: Option<String>,
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
) -> Vec<AlertRow> {
    let feed_timestamp = header.timestamp.unwrap_or(0);

    let cause = alert.cause.map(|raw| {
        transit_realtime::alert::Cause::try_from(raw)
            .unwrap()
            .as_str_name()
    });
    let effect = alert.effect.map(|raw| {
        transit_realtime::alert::Effect::try_from(raw)
            .unwrap()
            .as_str_name()
    });
    let severity_level = alert.severity_level.map(|raw| {
        transit_realtime::alert::SeverityLevel::try_from(raw)
            .unwrap()
            .as_str_name()
    });

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
    rows
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

pub fn decode_trip_update(
    tu: &transit_realtime::TripUpdate,
    header: &transit_realtime::FeedHeader,
) -> Vec<StopTimeUpdateRow> {
    let trip_id = tu.trip.trip_id.clone();
    let route_id = tu.trip.route_id.clone();
    let direction_id = tu.trip.direction_id;
    let start_date = tu.trip.start_date.clone();
    let start_time = tu.trip.start_time.clone();
    let schedule_relationship = tu.trip.schedule_relationship.map(|raw| {
        transit_realtime::trip_descriptor::ScheduleRelationship::try_from(raw)
            .unwrap()
            .as_str_name()
    });
    let vehicle_id = tu.vehicle.as_ref().and_then(|v| v.id.clone());
    let vehicle_label = tu.vehicle.as_ref().and_then(|v| v.label.clone());
    let feed_timestamp = header.timestamp.unwrap_or(0);
    let trip_update_timestamp = tu.timestamp;

    let mut rows = Vec::new();
    for stu in &tu.stop_time_update {
        rows.push(StopTimeUpdateRow {
            feed_timestamp,
            trip_update_timestamp,
            trip_id: trip_id.clone(),
            route_id: route_id.clone(),
            direction_id,
            start_date: start_date.clone(),
            start_time: start_time.clone(),
            schedule_relationship,
            vehicle_id: vehicle_id.clone(),
            vehicle_label: vehicle_label.clone(),
            stop_sequence: stu.stop_sequence,
            stop_id: stu.stop_id.clone(),
            arrival_delay: stu.arrival.as_ref().and_then(|a| a.delay),
            arrival_time: stu.arrival.as_ref().and_then(|a| a.time),
            arrival_uncertainty: stu.arrival.as_ref().and_then(|a| a.uncertainty),
            departure_delay: stu.departure.as_ref().and_then(|d| d.delay),
            departure_time: stu.departure.as_ref().and_then(|d| d.time),
            departure_uncertainty: stu.departure.as_ref().and_then(|d| d.uncertainty),
            stop_time_schedule_relationship: stu.schedule_relationship.map(|raw| {
                transit_realtime::trip_update::stop_time_update::ScheduleRelationship::try_from(raw)
                    .unwrap()
                    .as_str_name()
            }),
        });
    }
    rows
}

fn add(left: u64, right: u64) -> u64 {
    left + right
}

#[pyfunction]
fn py_add(left: u64, right: u64) -> u64 {
    add(left, right)
}

#[pymodule]
fn rail_decoder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_add, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn it_works() {
        assert_eq!(add(2, 2), 4);
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

#[cfg(test)]
mod decode_trip_update_tests {
    use super::*;
    use transit_realtime::{
        FeedHeader, TripDescriptor, TripUpdate, VehicleDescriptor,
        trip_descriptor::ScheduleRelationship, trip_update::StopTimeEvent,
        trip_update::StopTimeUpdate,
    };

    /// Mirrors Python's `_make_trip_update_feed()` defaults: one TripUpdate
    /// exploding into three StopTimeUpdateRows, matching
    /// TestStandardDecoderTripUpdates.test_one_stop_time_update_per_row /
    /// test_trip_level_fields_repeated_across_rows.
    #[test]
    fn explodes_one_row_per_stop_time_update_with_trip_fields_repeated() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let tu = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                route_id: Some("route-A".to_string()),
                direction_id: Some(0),
                start_date: Some("20240101".to_string()),
                start_time: Some("08:15:00".to_string()),
                schedule_relationship: Some(ScheduleRelationship::Scheduled as i32),
                ..Default::default()
            },
            vehicle: Some(VehicleDescriptor {
                id: Some("v1".to_string()),
                ..Default::default()
            }),
            timestamp: Some(1_700_000_030),
            stop_time_update: vec![
                StopTimeUpdate {
                    stop_id: Some("stop-1".to_string()),
                    arrival: Some(StopTimeEvent {
                        time: Some(1_700_000_100),
                        ..Default::default()
                    }),
                    ..Default::default()
                },
                StopTimeUpdate {
                    stop_id: Some("stop-2".to_string()),
                    arrival: Some(StopTimeEvent {
                        time: Some(1_700_000_200),
                        ..Default::default()
                    }),
                    ..Default::default()
                },
                StopTimeUpdate {
                    stop_id: Some("stop-3".to_string()),
                    arrival: Some(StopTimeEvent {
                        time: Some(1_700_000_300),
                        ..Default::default()
                    }),
                    ..Default::default()
                },
            ],
            ..Default::default()
        };

        let rows = decode_trip_update(&tu, &header);

        assert_eq!(rows.len(), 3);
        assert_eq!(
            rows.iter().map(|r| r.stop_id.clone()).collect::<Vec<_>>(),
            vec![
                Some("stop-1".to_string()),
                Some("stop-2".to_string()),
                Some("stop-3".to_string()),
            ]
        );
        // trip-level fields repeated identically across every exploded row
        assert!(rows.iter().all(|r| r.trip_id.as_deref() == Some("trip-1")));
        assert!(
            rows.iter()
                .all(|r| r.route_id.as_deref() == Some("route-A"))
        );
        assert!(
            rows.iter()
                .all(|r| r.start_time.as_deref() == Some("08:15:00"))
        );
        assert!(
            rows.iter()
                .all(|r| r.trip_update_timestamp == Some(1_700_000_030))
        );
        assert!(rows.iter().all(|r| r.feed_timestamp == 1_700_000_000));

        assert_eq!(rows[1].arrival_time, Some(1_700_000_200));
    }

    /// Mirrors test_missing_trip_update_timestamp_is_none: a minimal
    /// TripUpdate with only trip_id and a single bare stop_time_update
    /// (just stop_id, no arrival/departure at all).
    #[test]
    fn missing_trip_update_timestamp_and_stop_events_are_none() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let tu = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-min".to_string()),
                ..Default::default()
            },
            timestamp: None,
            stop_time_update: vec![StopTimeUpdate {
                stop_id: Some("stop-min".to_string()),
                ..Default::default()
            }],
            ..Default::default()
        };

        let rows = decode_trip_update(&tu, &header);

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].trip_update_timestamp, None);
        assert_eq!(rows[0].vehicle_id, None);
        assert_eq!(rows[0].route_id, None);
        assert_eq!(rows[0].schedule_relationship, None);
        assert_eq!(rows[0].arrival_delay, None);
        assert_eq!(rows[0].arrival_time, None);
        assert_eq!(rows[0].arrival_uncertainty, None);
        assert_eq!(rows[0].departure_delay, None);
        assert_eq!(rows[0].departure_time, None);
        assert_eq!(rows[0].departure_uncertainty, None);
    }

    /// A TripUpdate with zero stop_time_updates must yield zero rows —
    /// nothing in the Python suite exercises this directly (their minimal
    /// fixture always has exactly one stop_time_update), but it's the
    /// natural boundary of the `for stu in &tu.stop_time_update` loop and
    /// worth locking in on the Rust side.
    #[test]
    fn empty_stop_time_updates_yields_no_rows() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let tu = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-empty".to_string()),
                ..Default::default()
            },
            stop_time_update: vec![],
            ..Default::default()
        };

        assert!(decode_trip_update(&tu, &header).is_empty());
    }

    /// Targets the arrival/departure StopTimeEvent split specifically:
    /// arrival fully populated, departure entirely absent, on a single
    /// stop_time_update. Proves the two `Option<StopTimeEvent>` chains are
    /// independent rather than accidentally sharing state.
    #[test]
    fn arrival_and_departure_are_decoded_independently() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };

        let tu = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                ..Default::default()
            },
            stop_time_update: vec![StopTimeUpdate {
                stop_sequence: Some(5),
                stop_id: Some("stop-5".to_string()),
                arrival: Some(StopTimeEvent {
                    delay: Some(30),
                    time: Some(1_700_000_100),
                    uncertainty: Some(60),
                }),
                departure: None,
                ..Default::default()
            }],
            ..Default::default()
        };

        let rows = decode_trip_update(&tu, &header);

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].arrival_delay, Some(30));
        assert_eq!(rows[0].arrival_time, Some(1_700_000_100));
        assert_eq!(rows[0].arrival_uncertainty, Some(60));
        assert_eq!(rows[0].departure_delay, None);
        assert_eq!(rows[0].departure_time, None);
        assert_eq!(rows[0].departure_uncertainty, None);
    }
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

        let rows = decode_alert(&alert, "alert-1", &header);

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

        assert!(decode_alert(&alert, "alert-empty", &header).is_empty());
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

        let rows = decode_alert(&alert, "alert-i18n", &header);
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

        let rows = decode_alert(&alert, "alert-fr-only", &header);
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

        let rows = decode_alert(&alert, "alert-no-url", &header);
        assert_eq!(rows[0].url, None);
    }
}
