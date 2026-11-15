use alert::{AlertRow, AlertRowBuilder, decode_alert};
use arrow::array::RecordBatch;
use arrow::pyarrow::ToPyArrow;
use prost::Message;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use trip_update::{StopTimeUpdateRow, StopTimeUpdateRowBuilder, decode_trip_update};
use vehicle::{VehicleRow, VehicleRowBuilder, decode_vehicle};

mod alert;
mod enums;
mod nyct;
mod nyct_realtime;
mod tfnsw;
mod tfnsw_realtime;
mod transit_realtime;
mod trip_update;
mod vehicle;

/// The three canonical tables for one feed, keyed by `TableSpec.name`.
/// Shared by `decode_arrow` and the NYCT path, which extends these batches.
pub(crate) fn decode_canonical(
    feed: &transit_realtime::FeedMessage,
) -> Vec<(&'static str, RecordBatch)> {
    let mut vehicles = VehicleRowBuilder::new();
    let mut trip_updates = StopTimeUpdateRowBuilder::new();
    let mut alerts = AlertRowBuilder::new();

    for entity in &feed.entity {
        if let Some(vp) = &entity.vehicle {
            vehicles.append(vp, &feed.header);
        } else if let Some(tu) = &entity.trip_update {
            trip_updates.append(tu, &feed.header);
        } else if let Some(alert) = &entity.alert {
            alerts.append(alert, &entity.id, &feed.header);
        }
    }
    vec![
        ("vehicles", vehicles.finish()),
        ("trip_updates", trip_updates.finish()),
        ("alerts", alerts.finish()),
    ]
}

/// Decode one canonical GTFS-RT payload into {table name: RecordBatch}.
///
/// Raises ValueError only when the bytes are not a valid FeedMessage. An enum
/// value the proto doesn't define becomes null, matching the Python decoder.
/// The number of such values per table is in each batch's schema metadata
/// under `unknown_enum_values` (see crate::enums).
#[pyfunction]
fn decode_arrow<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let feed = transit_realtime::FeedMessage::decode(bytes)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    batches_to_pydict(py, decode_canonical(&feed))
}

#[pyclass(skip_from_py_object)]
pub struct DecodedRows {
    #[pyo3(get)]
    pub vehicles: Vec<VehicleRow>,
    #[pyo3(get)]
    pub stop_time_updates: Vec<StopTimeUpdateRow>,
    #[pyo3(get)]
    pub alerts: Vec<AlertRow>,
}

pub fn decode_feed_message(feed: &transit_realtime::FeedMessage) -> DecodedRows {
    let mut vehicles = Vec::new();
    let mut stop_time_updates = Vec::new();
    let mut alerts = Vec::new();

    for entity in &feed.entity {
        if let Some(vp) = &entity.vehicle {
            vehicles.push(decode_vehicle(vp, &feed.header));
        } else if let Some(tu) = &entity.trip_update {
            stop_time_updates.extend(decode_trip_update(tu, &feed.header));
        } else if let Some(alert) = &entity.alert {
            alerts.extend(decode_alert(alert, &entity.id, &feed.header));
        }
    }

    DecodedRows {
        vehicles,
        stop_time_updates,
        alerts,
    }
}

#[pyfunction]
fn decode(bytes: &[u8]) -> PyResult<DecodedRows> {
    let feed = transit_realtime::FeedMessage::decode(bytes)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(decode_feed_message(&feed))
}

fn batches_to_pydict<'py>(
    py: Python<'py>,
    batches: Vec<(&'static str, RecordBatch)>,
) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    for (name, batch) in batches {
        out.set_item(name, batch.to_pyarrow(py)?)?;
    }
    Ok(out)
}

#[pyfunction]
fn decode_arrow_tfnsw<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let feed = tfnsw_realtime::FeedMessage::decode(bytes)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    batches_to_pydict(py, tfnsw::decode_feed(&feed))
}

/// NYCT subway: canonical tables plus the field-1001 extensions. See src/nyct.
#[pyfunction]
fn decode_arrow_nyct<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let batches = nyct::decode_feed(bytes).map_err(PyValueError::new_err)?;
    batches_to_pydict(py, batches)
}

#[pymodule]
fn rail_decoder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decode, m)?)?;
    m.add_function(wrap_pyfunction!(decode_arrow, m)?)?;
    m.add_function(wrap_pyfunction!(decode_arrow_tfnsw, m)?)?;
    m.add_function(wrap_pyfunction!(decode_arrow_nyct, m)?)?;
    m.add_class::<VehicleRow>()?;
    m.add_class::<StopTimeUpdateRow>()?;
    m.add_class::<AlertRow>()?;
    m.add_class::<DecodedRows>()?;
    Ok(())
}
