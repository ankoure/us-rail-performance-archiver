use alert::{AlertRow, AlertRowBuilder, decode_alert};
use arrow::pyarrow::ToPyArrow;
use prost::Message;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use trip_update::{StopTimeUpdateRow, StopTimeUpdateRowBuilder, decode_trip_update};
use vehicle::{VehicleRow, VehicleRowBuilder, decode_vehicle};

mod alert;
mod tfnsw;
mod tfnsw_realtime;
mod transit_realtime;
mod trip_update;
mod vehicle;

#[pyfunction]
fn decode_arrow<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let feed = transit_realtime::FeedMessage::decode(bytes)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;

    let mut vehicles = VehicleRowBuilder::new();
    let mut trip_updates = StopTimeUpdateRowBuilder::new();
    let mut alerts = AlertRowBuilder::new();

    for entity in &feed.entity {
        if let Some(vp) = &entity.vehicle {
            vehicles
                .append(vp, &feed.header)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
        } else if let Some(tu) = &entity.trip_update {
            trip_updates
                .append(tu, &feed.header)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
        } else if let Some(alert) = &entity.alert {
            alerts
                .append(alert, &entity.id, &feed.header)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
        }
    }
    let out = PyDict::new(py);
    for (name, batch) in [
        ("vehicles", vehicles.finish()),
        ("trip_updates", trip_updates.finish()),
        ("alerts", alerts.finish()),
    ] {
        out.set_item(name, batch.to_pyarrow(py)?)?;
    }
    Ok(out)
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

pub fn decode_feed_message(
    feed: &transit_realtime::FeedMessage,
) -> Result<DecodedRows, prost::UnknownEnumValue> {
    let mut vehicles = Vec::new();
    let mut stop_time_updates = Vec::new();
    let mut alerts = Vec::new();

    for entity in &feed.entity {
        if let Some(vp) = &entity.vehicle {
            vehicles.push(decode_vehicle(vp, &feed.header)?);
        } else if let Some(tu) = &entity.trip_update {
            stop_time_updates.extend(decode_trip_update(tu, &feed.header)?);
        } else if let Some(alert) = &entity.alert {
            alerts.extend(decode_alert(alert, &entity.id, &feed.header)?);
        }
    }

    Ok(DecodedRows {
        vehicles,
        stop_time_updates,
        alerts,
    })
}

#[pyfunction]
fn decode(bytes: &[u8]) -> PyResult<DecodedRows> {
    let feed = transit_realtime::FeedMessage::decode(bytes)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    decode_feed_message(&feed).map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pymodule]
fn rail_decoder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decode, m)?)?;
    m.add_function(wrap_pyfunction!(decode_arrow, m)?)?;
    m.add_class::<VehicleRow>()?;
    m.add_class::<StopTimeUpdateRow>()?;
    m.add_class::<AlertRow>()?;
    m.add_class::<DecodedRows>()?;
    Ok(())
}
