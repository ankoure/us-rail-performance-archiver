fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Two schemas, two packages, two generated modules: transit_realtime.rs
    // and tfnsw_realtime.rs. They must not share a package name -- see
    // proto/tfnsw/README.md.
    prost_build::compile_protos(
        &[
            "proto/gtfs-realtime.proto",
            "proto/tfnsw/tfnsw-realtime.proto",
        ],
        &["proto/"],
    )?;
    Ok(())
}
