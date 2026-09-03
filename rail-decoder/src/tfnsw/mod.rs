//! TfNSW decode path.
//!
//! Parallel to the crate-root `vehicle`/`trip_update`/`alert` modules but over
//! `crate::tfnsw_realtime` types, plus a fourth table (`consist`) that has no
//! canonical equivalent.

pub mod alert;
pub mod consist;
pub mod trip_update;
pub mod vehicle;

use crate::tfnsw_realtime;
use alert::TfnswAlertRowBuilder;
use consist::ConsistRowBuilder;
use trip_update::TfnswStopTimeUpdateRowBuilder;
use vehicle::TfnswVehicleRowBuilder;

/// Decode one TfNSW payload into the four tables.
///
/// Returns batches keyed by `TableSpec.name` so the caller can drive writers
/// off `feed.decoder.produces` instead of unpacking a fixed-arity tuple. This
/// is the shape the canonical `decode_arrow` needs to move to as well -- see
/// the seam-change checklist.
///
/// All four keys are always present. An empty table comes back as a 0-row
/// batch carrying its schema, which leaves "absent from the map" free to mean
/// "this decoder does not produce this table at all".
///
/// Note `entity.update` (UpdateBundle, tag 1007) is deliberately unhandled --
/// it describes static-bundle cancellations, not a realtime observation, and
/// has no row type. Revisit only if it turns up populated in real payloads.
pub fn decode_feed(
    feed: &tfnsw_realtime::FeedMessage,
) -> Result<Vec<(&'static str, arrow::array::RecordBatch)>, prost::UnknownEnumValue> {
    let mut vehicles = TfnswVehicleRowBuilder::new();
    let mut stop_time_updates = TfnswStopTimeUpdateRowBuilder::new();
    let mut alerts = TfnswAlertRowBuilder::new();
    let mut consist = ConsistRowBuilder::new();

    for entity in &feed.entity {
        if let Some(vp) = &entity.vehicle {
            vehicles.append(vp, &feed.header, &mut consist)?;
        } else if let Some(tu) = &entity.trip_update {
            stop_time_updates.append(tu, &feed.header, &mut consist)?;
        } else if let Some(a) = &entity.alert {
            alerts.append(a, &entity.id, &feed.header)?;
        }
    }

    // Keyed by TableSpec.name, not by row-class grain: the stop-time-update
    // builder feeds the "trip_updates" table. Emission order here becomes the
    // dict insertion order at the Python seam.
    Ok(vec![
        ("vehicles", vehicles.finish()),
        ("trip_updates", stop_time_updates.finish()),
        ("alerts", alerts.finish()),
        ("consist", consist.finish()),
    ])
}
