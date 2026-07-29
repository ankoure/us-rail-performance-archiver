use alert::{AlertRow, decode_alert};
use prost::Message;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use trip_update::{StopTimeUpdateRow, decode_trip_update};
use vehicle::{VehicleRow, decode_vehicle};

mod alert;
mod transit_realtime;
mod trip_update;
mod vehicle;

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

#[pymodule]
fn rail_decoder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decode, m)?)?;
    m.add_class::<VehicleRow>()?;
    m.add_class::<StopTimeUpdateRow>()?;
    m.add_class::<AlertRow>()?;
    m.add_class::<DecodedRows>()?;
    Ok(())
}
