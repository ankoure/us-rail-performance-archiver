//! NYCT subway decode path: canonical GTFS-RT plus the field-1001 extensions.
//!
//! Each payload is decoded twice. The canonical decode goes through the
//! unchanged crate-root builders. The second decode uses the `nyct_realtime`
//! overlay, which declares only the path to each 1001 field (see
//! proto/nyct/README.md). Both decodes read the same bytes, so the overlay's
//! entities and stop-time-updates line up with the canonical rows by position.
//! The walk below mirrors `decode_canonical`'s `vehicle`-before-`trip_update`
//! precedence, and `extend_batch` refuses to merge if row counts disagree.
//!
//! Besides adding `nyct_*` columns, this path *derives* `direction_id`. NYCT
//! publishes `direction_id = 0` on every trip as a placeholder, and the real
//! direction is `NyctTripDescriptor.direction`. That is mapped onto the static
//! GTFS convention (trips.txt: N → 0, S → 1) so that marts grouping on
//! `(route_id, direction_id)` stop mixing both directions. The raw enum is kept
//! in `nyct_direction`. EAST/WEST (unused by NYCT) and an absent or unknown
//! direction leave the published `direction_id` as-is.

use crate::{nyct_realtime, transit_realtime};
use arrow::array::{
    Array, ArrayRef, BooleanBuilder, RecordBatch, StringBuilder, UInt32Array, UInt64Builder,
};
use arrow::datatypes::{DataType, Field, Schema};
use nyct_realtime::nyct_trip_descriptor::Direction;
use prost::Message;
use std::sync::Arc;

pub const REPLACEMENT_PERIODS_TABLE: &str = "nyct_trip_replacement_periods";

/// Decode one NYCT payload into the canonical three tables (with NYCT columns
/// added to `vehicles` and `trip_updates`) plus the replacement-period table.
pub fn decode_feed(bytes: &[u8]) -> Result<Vec<(&'static str, RecordBatch)>, String> {
    let feed = transit_realtime::FeedMessage::decode(bytes).map_err(|e| e.to_string())?;
    let overlay = nyct_realtime::FeedMessage::decode(bytes).map_err(|e| e.to_string())?;

    let mut vehicle_cols = TripColumns::new();
    let mut stu_cols = TripColumns::new();
    let mut scheduled_track = StringBuilder::new();
    let mut actual_track = StringBuilder::new();

    for entity in &overlay.entity {
        if let Some(vp) = &entity.vehicle {
            vehicle_cols.append(nyct_trip(vp.trip.as_ref()));
        } else if let Some(tu) = &entity.trip_update {
            let nyct = nyct_trip(tu.trip.as_ref());
            for stu in &tu.stop_time_update {
                stu_cols.append(nyct);
                let ext = stu.nyct_stop_time_update.as_ref();
                scheduled_track.append_option(ext.and_then(|e| e.scheduled_track.as_deref()));
                actual_track.append_option(ext.and_then(|e| e.actual_track.as_deref()));
            }
        }
    }

    let mut vehicle_ext = Some(vehicle_cols.finish());
    let mut stu_ext = Some(stu_cols.finish()).map(|(overrides, mut extra)| {
        extra.push(utf8("nyct_scheduled_track", scheduled_track.finish()));
        extra.push(utf8("nyct_actual_track", actual_track.finish()));
        (overrides, extra)
    });

    let mut out = Vec::with_capacity(4);
    for (name, batch) in crate::decode_canonical(&feed) {
        let ext = match name {
            "vehicles" => vehicle_ext.take(),
            "trip_updates" => stu_ext.take(),
            _ => None,
        };
        let batch = match ext {
            Some((overrides, extra)) => extend_batch(name, batch, &overrides, extra)?,
            None => batch,
        };
        out.push((name, batch));
    }

    let feed_timestamp = feed.header.timestamp.unwrap_or(0);
    let nyct_header = overlay
        .header
        .as_ref()
        .and_then(|h| h.nyct_feed_header.as_ref());
    out.push((
        REPLACEMENT_PERIODS_TABLE,
        replacement_periods(feed_timestamp, nyct_header),
    ));
    Ok(out)
}

fn nyct_trip(
    trip: Option<&nyct_realtime::TripDescriptor>,
) -> Option<&nyct_realtime::NyctTripDescriptor> {
    trip.and_then(|t| t.nyct_trip_descriptor.as_ref())
}

/// NyctTripDescriptor.direction → static-GTFS direction_id, or None to keep
/// the published value.
fn derived_direction_id(direction: Option<Direction>) -> Option<u32> {
    match direction? {
        Direction::North => Some(0),
        Direction::South => Some(1),
        Direction::East | Direction::West => None,
    }
}

/// Trip-level NYCT columns, one row per canonical row.
struct TripColumns {
    train_id: StringBuilder,
    is_assigned: BooleanBuilder,
    direction: StringBuilder,
    direction_id_override: Vec<Option<u32>>,
}

impl TripColumns {
    fn new() -> Self {
        Self {
            train_id: StringBuilder::new(),
            is_assigned: BooleanBuilder::new(),
            direction: StringBuilder::new(),
            direction_id_override: Vec::new(),
        }
    }

    fn append(&mut self, nyct: Option<&nyct_realtime::NyctTripDescriptor>) {
        // An unknown enum value decodes to null rather than failing the payload
        // (cf. #121, where the canonical builders do fail it).
        let direction = nyct
            .and_then(|n| n.direction)
            .and_then(|v| Direction::try_from(v).ok());
        self.train_id
            .append_option(nyct.and_then(|n| n.train_id.as_deref()));
        self.is_assigned
            .append_option(nyct.and_then(|n| n.is_assigned));
        self.direction
            .append_option(direction.map(|d| d.as_str_name()));
        self.direction_id_override
            .push(derived_direction_id(direction));
    }

    fn finish(mut self) -> (Vec<Option<u32>>, Vec<(Field, ArrayRef)>) {
        let extra = vec![
            utf8("nyct_train_id", self.train_id.finish()),
            (
                Field::new("nyct_is_assigned", DataType::Boolean, true),
                Arc::new(self.is_assigned.finish()) as ArrayRef,
            ),
            utf8("nyct_direction", self.direction.finish()),
        ];
        (self.direction_id_override, extra)
    }
}

fn utf8(name: &str, array: arrow::array::StringArray) -> (Field, ArrayRef) {
    (Field::new(name, DataType::Utf8, true), Arc::new(array))
}

/// Append `extra` columns to a canonical batch and apply the `direction_id`
/// overrides. Errors if the overlay walk produced a different number of rows
/// than the canonical builders did -- the positional join would be wrong.
fn extend_batch(
    table: &str,
    batch: RecordBatch,
    direction_id_override: &[Option<u32>],
    extra: Vec<(Field, ArrayRef)>,
) -> Result<RecordBatch, String> {
    if batch.num_rows() != direction_id_override.len() {
        return Err(format!(
            "nyct {table}: overlay produced {} rows, canonical {}",
            direction_id_override.len(),
            batch.num_rows()
        ));
    }

    let schema = batch.schema();
    let idx = schema
        .index_of("direction_id")
        .map_err(|e| format!("nyct {table}: {e}"))?;
    let published = batch
        .column(idx)
        .as_any()
        .downcast_ref::<UInt32Array>()
        .ok_or_else(|| format!("nyct {table}: direction_id is not UInt32"))?;
    let direction_id: UInt32Array = direction_id_override
        .iter()
        .enumerate()
        .map(|(i, o)| o.or_else(|| published.is_valid(i).then(|| published.value(i))))
        .collect();

    let mut fields: Vec<Field> = schema.fields().iter().map(|f| f.as_ref().clone()).collect();
    let mut columns: Vec<ArrayRef> = batch.columns().to_vec();
    columns[idx] = Arc::new(direction_id);
    for (field, array) in extra {
        fields.push(field);
        columns.push(array);
    }
    // Keep the canonical schema metadata (e.g. unknown_enum_values, see crate::enums).
    let schema = Schema::new_with_metadata(fields, schema.metadata().clone());
    RecordBatch::try_new(Arc::new(schema), columns).map_err(|e| format!("nyct {table}: {e}"))
}

/// One row per `NyctFeedHeader.trip_replacement_period`. Within a period, a
/// scheduled trip on that route that is absent from the feed is cancelled,
/// as opposed to merely unreported.
fn replacement_periods(
    feed_timestamp: u64,
    header: Option<&nyct_realtime::NyctFeedHeader>,
) -> RecordBatch {
    let mut ts = UInt64Builder::new();
    let mut version = StringBuilder::new();
    let mut route_id = StringBuilder::new();
    let mut start = UInt64Builder::new();
    let mut end = UInt64Builder::new();

    if let Some(h) = header {
        for p in &h.trip_replacement_period {
            let range = p.replacement_period.as_ref();
            ts.append_value(feed_timestamp);
            version.append_option(h.nyct_subway_version.as_deref());
            route_id.append_option(p.route_id.as_deref());
            start.append_option(range.and_then(|r| r.start));
            end.append_option(range.and_then(|r| r.end));
        }
    }

    let schema = Schema::new(vec![
        Field::new("feed_timestamp", DataType::UInt64, false),
        Field::new("nyct_subway_version", DataType::Utf8, true),
        Field::new("route_id", DataType::Utf8, true),
        Field::new("replacement_start", DataType::UInt64, true),
        Field::new("replacement_end", DataType::UInt64, true),
    ]);
    RecordBatch::try_new(
        Arc::new(schema),
        vec![
            Arc::new(ts.finish()),
            Arc::new(version.finish()),
            Arc::new(route_id.finish()),
            Arc::new(start.finish()),
            Arc::new(end.finish()),
        ],
    )
    .expect("columns match schema by construction")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direction_maps_to_static_gtfs_convention() {
        assert_eq!(derived_direction_id(Some(Direction::North)), Some(0));
        assert_eq!(derived_direction_id(Some(Direction::South)), Some(1));
        assert_eq!(derived_direction_id(Some(Direction::East)), None);
        assert_eq!(derived_direction_id(None), None);
    }

    #[test]
    fn unknown_direction_is_null_not_an_error() {
        let mut cols = TripColumns::new();
        let nyct = nyct_realtime::NyctTripDescriptor {
            train_id: Some("x".into()),
            is_assigned: None,
            direction: Some(99),
        };
        cols.append(Some(&nyct));
        let (overrides, extra) = cols.finish();
        assert_eq!(overrides, vec![None]);
        let direction = &extra[2].1;
        assert!(direction.is_null(0));
    }
}
