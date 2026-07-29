use crate::transit_realtime;
use pyo3::prelude::*;

#[pyclass(skip_from_py_object)]
#[derive(Debug, Clone, PartialEq)]
pub struct StopTimeUpdateRow {
    #[pyo3(get)]
    pub feed_timestamp: u64,
    #[pyo3(get)]
    pub trip_update_timestamp: Option<u64>,
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
    pub vehicle_id: Option<String>,
    #[pyo3(get)]
    pub vehicle_label: Option<String>,
    #[pyo3(get)]
    pub stop_sequence: Option<u32>,
    #[pyo3(get)]
    pub stop_id: Option<String>,
    #[pyo3(get)]
    pub arrival_delay: Option<i32>,
    #[pyo3(get)]
    pub arrival_time: Option<i64>,
    #[pyo3(get)]
    pub arrival_uncertainty: Option<i32>,
    #[pyo3(get)]
    pub departure_delay: Option<i32>,
    #[pyo3(get)]
    pub departure_time: Option<i64>,
    #[pyo3(get)]
    pub departure_uncertainty: Option<i32>,
    #[pyo3(get)]
    pub stop_time_schedule_relationship: Option<&'static str>,
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
