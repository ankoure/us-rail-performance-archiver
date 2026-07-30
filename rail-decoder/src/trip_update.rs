use crate::transit_realtime::{self};
use arrow::array::{
    Int32Builder, Int64Builder, RecordBatch, StringBuilder, UInt32Builder, UInt64Builder,
};
use arrow::datatypes::{DataType, Field, Schema};
use pyo3::prelude::*;
use std::sync::Arc;

pub struct StopTimeUpdateRowBuilder {
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
}

impl StopTimeUpdateRowBuilder {
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
        }
    }

    pub fn append(
        &mut self,
        tu: &transit_realtime::TripUpdate,
        header: &transit_realtime::FeedHeader,
    ) -> Result<(), prost::UnknownEnumValue> {
        // Trip-level values, computed once — same expressions as decode_trip_update's
        // hoisted `let` bindings.
        let feed_timestamp = header.timestamp.unwrap_or(0);
        let trip_id = tu.trip.trip_id.clone();
        let route_id = tu.trip.route_id.clone();
        let direction_id = tu.trip.direction_id;
        let start_date = tu.trip.start_date.clone();
        let start_time = tu.trip.start_time.clone();
        let schedule_relationship = tu
            .trip
            .schedule_relationship
            .map(|raw| {
                transit_realtime::trip_descriptor::ScheduleRelationship::try_from(raw)
                    .map(|v| v.as_str_name())
            })
            .transpose()?;
        let vehicle_id = tu.vehicle.as_ref().and_then(|v| v.id.clone());
        let vehicle_label = tu.vehicle.as_ref().and_then(|v| v.label.clone());
        let trip_update_timestamp = tu.timestamp;

        for stu in &tu.stop_time_update {
            // Trip-level fields, repeated across every exploded row — each
            // .clone()'d in since they're reused across the loop.
            self.feed_timestamp.append_value(feed_timestamp);
            self.trip_update_timestamp
                .append_option(trip_update_timestamp);
            self.trip_id.append_option(trip_id.clone());
            self.route_id.append_option(route_id.clone());
            self.direction_id.append_option(direction_id);
            self.start_date.append_option(start_date.clone());
            self.start_time.append_option(start_time.clone());
            self.schedule_relationship
                .append_option(schedule_relationship);
            self.vehicle_id.append_option(vehicle_id.clone());
            self.vehicle_label.append_option(vehicle_label.clone());

            // Stop-level fields, fresh per row — same expressions as
            // decode_trip_update's per-stu logic.
            self.stop_sequence.append_option(stu.stop_sequence);
            self.stop_id.append_option(stu.stop_id.clone());
            self.arrival_delay
                .append_option(stu.arrival.as_ref().and_then(|a| a.delay));
            self.arrival_time
                .append_option(stu.arrival.as_ref().and_then(|a| a.time));
            self.arrival_uncertainty
                .append_option(stu.arrival.as_ref().and_then(|a| a.uncertainty));
            self.departure_delay
                .append_option(stu.departure.as_ref().and_then(|d| d.delay));
            self.departure_time
                .append_option(stu.departure.as_ref().and_then(|d| d.time));
            self.departure_uncertainty
                .append_option(stu.departure.as_ref().and_then(|d| d.uncertainty));
            self.stop_time_schedule_relationship.append_option(
                stu.schedule_relationship
                    .map(|raw| {
                        transit_realtime::trip_descriptor::ScheduleRelationship::try_from(raw)
                            .map(|v| v.as_str_name())
                    })
                    .transpose()?,
            )
        }
        Ok(())
    }

    pub fn finish(mut self) -> RecordBatch {
        let schema = Arc::new(Schema::new(vec![
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
        ]));

        RecordBatch::try_new(
            schema,
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
            ],
        )
        .unwrap()
    }
}

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
) -> Result<Vec<StopTimeUpdateRow>, prost::UnknownEnumValue> {
    let trip_id = tu.trip.trip_id.clone();
    let route_id = tu.trip.route_id.clone();
    let direction_id = tu.trip.direction_id;
    let start_date = tu.trip.start_date.clone();
    let start_time = tu.trip.start_time.clone();
    let schedule_relationship = tu
        .trip
        .schedule_relationship
        .map(|raw| {
            transit_realtime::trip_descriptor::ScheduleRelationship::try_from(raw)
                .map(|v| v.as_str_name())
        })
        .transpose()?;
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
            stop_time_schedule_relationship: stu
                .schedule_relationship
                .map(|raw| {
                    transit_realtime::trip_update::stop_time_update::ScheduleRelationship::try_from(
                        raw,
                    )
                    .map(|v| v.as_str_name())
                })
                .transpose()?,
        });
    }
    Ok(rows)
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

        let rows = decode_trip_update(&tu, &header).unwrap();

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

        let rows = decode_trip_update(&tu, &header).unwrap();

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

        assert!(decode_trip_update(&tu, &header).unwrap().is_empty());
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

        let rows = decode_trip_update(&tu, &header).unwrap();

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].arrival_delay, Some(30));
        assert_eq!(rows[0].arrival_time, Some(1_700_000_100));
        assert_eq!(rows[0].arrival_uncertainty, Some(60));
        assert_eq!(rows[0].departure_delay, None);
        assert_eq!(rows[0].departure_time, None);
        assert_eq!(rows[0].departure_uncertainty, None);
    }
    #[test]
    fn unrecognized_schedule_relationship_returns_err_not_panic() {
        let header = FeedHeader {
            timestamp: Some(1),
            ..Default::default()
        };
        let tu = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                schedule_relationship: Some(8), // out-of-spec value seen in real PRT (prt-trips) data
                ..Default::default()
            },
            stop_time_update: vec![StopTimeUpdate::default()],
            ..Default::default()
        };

        assert!(decode_trip_update(&tu, &header).is_err());
    }
}
