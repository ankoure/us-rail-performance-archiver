use crate::enums::{enum_name, enum_name_uncounted, with_unknown_count};
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
    unknown_enum_values: u64,
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
            unknown_enum_values: 0,
        }
    }

    pub fn append(
        &mut self,
        tu: &transit_realtime::TripUpdate,
        header: &transit_realtime::FeedHeader,
    ) {
        use transit_realtime::trip_descriptor::ScheduleRelationship as TripRel;
        use transit_realtime::trip_update::stop_time_update::ScheduleRelationship as StuRel;

        // Trip-level values, computed once — same expressions as decode_trip_update's
        // hoisted `let` bindings.
        let feed_timestamp = header.timestamp.unwrap_or(0);
        let trip_id = tu.trip.trip_id.clone();
        let route_id = tu.trip.route_id.clone();
        let direction_id = tu.trip.direction_id;
        let start_date = tu.trip.start_date.clone();
        let start_time = tu.trip.start_time.clone();
        // An unknown enum value becomes null and is counted (see crate::enums).
        let schedule_relationship = enum_name(
            tu.trip.schedule_relationship,
            TripRel::as_str_name,
            &mut self.unknown_enum_values,
        );
        let vehicle_id = tu.vehicle.as_ref().and_then(|v| v.id.clone());
        let vehicle_label = tu.vehicle.as_ref().and_then(|v| v.label.clone());
        let trip_update_timestamp = tu.timestamp;

        for stu in &tu.stop_time_update {
            // Resolved before this row's first append, like the trip-level
            // name above.
            let stop_time_schedule_relationship = enum_name(
                stu.schedule_relationship,
                StuRel::as_str_name,
                &mut self.unknown_enum_values,
            );

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
            self.stop_time_schedule_relationship
                .append_option(stop_time_schedule_relationship);
        }
    }

    pub fn finish(mut self) -> RecordBatch {
        let schema = Arc::new(with_unknown_count(
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
            ]),
            self.unknown_enum_values,
        ));

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
) -> Vec<StopTimeUpdateRow> {
    use transit_realtime::trip_descriptor::ScheduleRelationship as TripRel;
    use transit_realtime::trip_update::stop_time_update::ScheduleRelationship as StuRel;

    let trip_id = tu.trip.trip_id.clone();
    let route_id = tu.trip.route_id.clone();
    let direction_id = tu.trip.direction_id;
    let start_date = tu.trip.start_date.clone();
    let start_time = tu.trip.start_time.clone();
    let schedule_relationship =
        enum_name_uncounted(tu.trip.schedule_relationship, TripRel::as_str_name);
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
            stop_time_schedule_relationship: enum_name_uncounted(
                stu.schedule_relationship,
                StuRel::as_str_name,
            ),
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
                    ..Default::default()
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
    #[test]
    fn unrecognized_schedule_relationship_becomes_none_and_keeps_the_row() {
        let header = FeedHeader {
            timestamp: Some(1),
            ..Default::default()
        };
        let tu = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                // Not in the proto. 8 used to be the example here (seen in real
                // prt-trips data) until the proto picked up NEW = 8.
                schedule_relationship: Some(99),
                ..Default::default()
            },
            stop_time_update: vec![StopTimeUpdate::default()],
            ..Default::default()
        };

        let rows = decode_trip_update(&tu, &header);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].trip_id.as_deref(), Some("trip-1"));
        assert_eq!(rows[0].schedule_relationship, None);
    }

    #[test]
    fn new_and_deleted_schedule_relationships_resolve() {
        let header = FeedHeader::default();
        for (raw, name) in [(7, "DELETED"), (8, "NEW")] {
            let tu = TripUpdate {
                trip: TripDescriptor {
                    schedule_relationship: Some(raw),
                    ..Default::default()
                },
                stop_time_update: vec![StopTimeUpdate::default()],
                ..Default::default()
            };
            assert_eq!(
                decode_trip_update(&tu, &header)[0].schedule_relationship,
                Some(name)
            );
        }
    }
}

#[cfg(test)]
mod stop_time_update_row_builder_tests {
    use super::*;
    use crate::enums::UNKNOWN_ENUM_VALUES_KEY;
    use arrow::array::{Array, StringArray};
    use transit_realtime::{
        FeedHeader, TripDescriptor, TripUpdate, VehicleDescriptor,
        trip_descriptor::ScheduleRelationship, trip_update::StopTimeEvent,
        trip_update::StopTimeUpdate,
    };

    fn string_col<'a>(batch: &'a RecordBatch, name: &str) -> &'a StringArray {
        batch
            .column_by_name(name)
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap()
    }

    fn unknown_count(batch: &RecordBatch) -> &str {
        batch.schema_ref().metadata()[UNKNOWN_ENUM_VALUES_KEY].as_str()
    }

    fn stu(schedule_relationship: Option<i32>) -> StopTimeUpdate {
        StopTimeUpdate {
            schedule_relationship,
            ..Default::default()
        }
    }

    /// Stop-level schedule_relationship is StopTimeUpdate.ScheduleRelationship,
    /// not TripDescriptor's: 1 is SKIPPED, not ADDED.
    #[test]
    fn stop_time_schedule_relationship_uses_the_stop_level_enum() {
        let tu = TripUpdate {
            trip: TripDescriptor::default(),
            stop_time_update: vec![stu(Some(1)), stu(Some(2)), stu(Some(3))],
            ..Default::default()
        };
        let mut builder = StopTimeUpdateRowBuilder::new();
        builder.append(&tu, &FeedHeader::default());
        let batch = builder.finish();

        let col = string_col(&batch, "stop_time_schedule_relationship");
        assert_eq!(col.value(0), "SKIPPED");
        assert_eq!(col.value(1), "NO_DATA");
        assert_eq!(col.value(2), "UNSCHEDULED");
        assert_eq!(unknown_count(&batch), "0");
    }

    /// One unknown value nulls that one field and is counted. Every row survives
    /// and every column stays the same length.
    #[test]
    fn unknown_values_become_null_and_are_counted() {
        let header = FeedHeader::default();
        let bad_trip = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("bad-trip".to_string()),
                schedule_relationship: Some(99),
                ..Default::default()
            },
            stop_time_update: vec![stu(Some(0)), stu(Some(42))],
            ..Default::default()
        };
        let good_trip = TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("good-trip".to_string()),
                schedule_relationship: Some(0),
                ..Default::default()
            },
            stop_time_update: vec![stu(Some(1))],
            ..Default::default()
        };

        let mut builder = StopTimeUpdateRowBuilder::new();
        builder.append(&bad_trip, &header);
        builder.append(&good_trip, &header);
        let batch = builder.finish();

        assert_eq!(batch.num_rows(), 3);
        let trip_rel = string_col(&batch, "schedule_relationship");
        assert!(trip_rel.is_null(0) && trip_rel.is_null(1));
        assert_eq!(trip_rel.value(2), "SCHEDULED");
        let stop_rel = string_col(&batch, "stop_time_schedule_relationship");
        assert_eq!(stop_rel.value(0), "SCHEDULED");
        assert!(stop_rel.is_null(1));
        assert_eq!(stop_rel.value(2), "SKIPPED");
        // Counted once per value in the payload, not once per exploded row:
        // the bad trip-level value covers two rows but counts once.
        assert_eq!(unknown_count(&batch), "2");
    }

    /// One stop per StopTimeUpdate.ScheduleRelationship value, plus an unset
    /// one, under a non-SCHEDULED trip so the trip- and stop-level enums are
    /// both exercised. ADDED is deprecated upstream but still sent by feeds.
    #[allow(deprecated)]
    fn mixed_schedule_relationship_trip_update() -> TripUpdate {
        use transit_realtime::trip_update::stop_time_update::ScheduleRelationship as Stu;
        let stop = |seq: u32, rel: Option<Stu>| StopTimeUpdate {
            stop_sequence: Some(seq),
            stop_id: Some(format!("stop-{seq}")),
            arrival: Some(StopTimeEvent {
                delay: Some(seq as i32),
                time: Some(1_700_000_000 + seq as i64),
                uncertainty: None,
                ..Default::default()
            }),
            schedule_relationship: rel.map(|r| r as i32),
            ..Default::default()
        };
        TripUpdate {
            trip: TripDescriptor {
                trip_id: Some("trip-1".to_string()),
                route_id: Some("route-A".to_string()),
                direction_id: Some(1),
                start_date: Some("20240101".to_string()),
                start_time: Some("08:15:00".to_string()),
                schedule_relationship: Some(ScheduleRelationship::Added as i32),
                ..Default::default()
            },
            vehicle: Some(VehicleDescriptor {
                id: Some("v1".to_string()),
                label: Some("Train 1".to_string()),
                ..Default::default()
            }),
            timestamp: Some(1_700_000_030),
            stop_time_update: vec![
                stop(1, Some(Stu::Scheduled)),
                stop(2, Some(Stu::Skipped)),
                stop(3, Some(Stu::NoData)),
                stop(4, Some(Stu::Unscheduled)),
                stop(5, None),
            ],
            ..Default::default()
        }
    }

    const EXPECTED_STOP_RELATIONSHIPS: [Option<&str>; 5] = [
        Some("SCHEDULED"),
        Some("SKIPPED"),
        Some("NO_DATA"),
        Some("UNSCHEDULED"),
        None,
    ];

    #[test]
    fn stop_time_schedule_relationship_uses_stop_time_update_enum() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };
        let rows = decode_trip_update(&mixed_schedule_relationship_trip_update(), &header);

        assert_eq!(
            rows.iter()
                .map(|r| r.stop_time_schedule_relationship)
                .collect::<Vec<_>>(),
            EXPECTED_STOP_RELATIONSHIPS
        );
        assert!(
            rows.iter()
                .all(|r| r.schedule_relationship == Some("ADDED"))
        );
    }

    /// Regression for #120: the arrow builder decoded the stop-level field
    /// with the TripDescriptor enum (SKIPPED -> "ADDED").
    #[test]
    fn arrow_stop_time_schedule_relationship_uses_stop_time_update_enum() {
        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };
        let mut builder = StopTimeUpdateRowBuilder::new();
        builder.append(&mixed_schedule_relationship_trip_update(), &header);
        let batch = builder.finish();

        let col = batch
            .column_by_name("stop_time_schedule_relationship")
            .unwrap()
            .as_any()
            .downcast_ref::<StringArray>()
            .unwrap();
        let actual: Vec<Option<&str>> = (0..col.len())
            .map(|i| col.is_valid(i).then(|| col.value(i)))
            .collect();
        assert_eq!(actual, EXPECTED_STOP_RELATIONSHIPS);
    }

    /// `decode` (rows) and `decode_arrow` (builder) must agree on every
    /// column. Both sides are stringified (arrow's cast on one, to_string on
    /// the other) so one comparison covers every type; a column added to only
    /// one side fails the width check.
    #[test]
    fn builder_matches_decode_trip_update_on_every_column() {
        use arrow::compute::cast;
        use arrow::datatypes::DataType;

        let header = FeedHeader {
            timestamp: Some(1_700_000_000),
            ..Default::default()
        };
        let tu = mixed_schedule_relationship_trip_update();

        let rows = decode_trip_update(&tu, &header);
        let mut builder = StopTimeUpdateRowBuilder::new();
        builder.append(&tu, &header);
        let batch = builder.finish();

        fn s<T: ToString>(v: Option<T>) -> Option<String> {
            v.map(|v| v.to_string())
        }
        let expected: Vec<Vec<Option<String>>> = rows
            .iter()
            .map(|r| {
                vec![
                    Some(r.feed_timestamp.to_string()),
                    s(r.trip_update_timestamp),
                    r.trip_id.clone(),
                    r.route_id.clone(),
                    s(r.direction_id),
                    r.start_date.clone(),
                    r.start_time.clone(),
                    s(r.schedule_relationship),
                    r.vehicle_id.clone(),
                    r.vehicle_label.clone(),
                    s(r.stop_sequence),
                    r.stop_id.clone(),
                    s(r.arrival_delay),
                    s(r.arrival_time),
                    s(r.arrival_uncertainty),
                    s(r.departure_delay),
                    s(r.departure_time),
                    s(r.departure_uncertainty),
                    s(r.stop_time_schedule_relationship),
                ]
            })
            .collect();

        assert_eq!(batch.num_rows(), rows.len());
        assert_eq!(batch.num_columns(), expected[0].len());

        let columns: Vec<StringArray> = batch
            .columns()
            .iter()
            .map(|c| {
                cast(c, &DataType::Utf8)
                    .unwrap()
                    .as_any()
                    .downcast_ref::<StringArray>()
                    .unwrap()
                    .clone()
            })
            .collect();
        let actual: Vec<Vec<Option<String>>> = (0..batch.num_rows())
            .map(|i| {
                columns
                    .iter()
                    .map(|c| c.is_valid(i).then(|| c.value(i).to_string()))
                    .collect()
            })
            .collect();

        assert_eq!(actual, expected);
    }
}
