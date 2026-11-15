//! Raw proto enum values -> their string names, the same way the Python
//! decoder does it.
//!
//! prost stores proto2 enum fields as a raw `i32`, so a value the vendored
//! .proto doesn't define still decodes. Python's upb treats that same value
//! as unset, so the field comes out `None` and the row is kept. `enum_name`
//! does the same: an unknown value becomes `None`. It is not an error, since
//! an error here would throw away every row in the payload. Each unknown
//! value also bumps the caller's counter, so dropping it is never silent.

use arrow::datatypes::Schema;
use std::collections::HashMap;

/// Schema metadata key that carries a builder's unknown-enum count across the
/// Python seam. It holds a decimal string, because Arrow metadata is
/// string -> string.
pub const UNKNOWN_ENUM_VALUES_KEY: &str = "unknown_enum_values";

/// Name of `raw` as enum `E`, or `None` if `raw` is absent or `E` has no such
/// value. An unknown value increments `unknown`.
///
/// `as_str_name` is passed in because prost generates it as an inherent
/// method, not a trait method.
pub fn enum_name<E: TryFrom<i32>>(
    raw: Option<i32>,
    as_str_name: fn(&E) -> &'static str,
    unknown: &mut u64,
) -> Option<&'static str> {
    match E::try_from(raw?) {
        Ok(v) => Some(as_str_name(&v)),
        Err(_) => {
            *unknown += 1;
            None
        }
    }
}

/// `enum_name` for callers that have nowhere to report the count (the
/// row-object `decode` API).
pub fn enum_name_uncounted<E: TryFrom<i32>>(
    raw: Option<i32>,
    as_str_name: fn(&E) -> &'static str,
) -> Option<&'static str> {
    enum_name(raw, as_str_name, &mut 0)
}

/// `schema` with the unknown-enum count attached as metadata.
pub fn with_unknown_count(schema: Schema, unknown: u64) -> Schema {
    schema.with_metadata(HashMap::from([(
        UNKNOWN_ENUM_VALUES_KEY.to_string(),
        unknown.to_string(),
    )]))
}
