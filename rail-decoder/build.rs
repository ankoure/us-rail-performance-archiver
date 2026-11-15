fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Three schemas, three packages, three generated modules:
    // transit_realtime.rs, tfnsw_realtime.rs and nyct_realtime.rs. They must
    // not share a package name -- see proto/tfnsw/README.md and
    // proto/nyct/README.md.
    prost_build::compile_protos(
        &[
            "proto/gtfs-realtime.proto",
            "proto/tfnsw/tfnsw-realtime.proto",
            "proto/nyct/nyct-overlay.proto",
        ],
        &["proto/"],
    )?;
    Ok(())
}
