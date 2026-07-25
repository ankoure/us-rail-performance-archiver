use pyo3::prelude::*;

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
