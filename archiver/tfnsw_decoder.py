"""TfNSW decoder skeleton -- row types and table specs for the four TfNSW tables.

SKELETON. Not yet imported by archiver/decoder.py, and no feed references it,
so importing this module changes nothing at runtime. Fold it into decoder.py
(or import it there) when you're ready to register it.

Why a separate decoder at all: TfNSW is its own GTFS-RT permutation, not an
extension. `StopTimeUpdate` field 6 is `departure_occupancy_status` (varint)
where canonical uses it for `stop_time_properties` (submessage), so the
canonical decoder cannot read it -- measured at ~1919 of 4439 stop time updates
lost on a live Sydney Trains payload. Carriage data (tags 1007) is invisible to
canonical entirely: ~1883 observed + ~15,020 predicted carriages per payload.
"""

from dataclasses import dataclass
from typing import ClassVar

from archiver.decoder import (
    AlertRow,
    Decoder,
    GtfsRtDecoder,
    Row,
    StopTimeUpdateRow,
    TableSpec,
    VehicleRow,
)


@dataclass
class ConsistRow(Row):
    """One carriage, from either TfNSW consist stream.

    `origin` distinguishes them: "vehicle_position" (observed now) or
    "stop_time_update" (predicted at a future stop). stop_sequence/stop_id are
    None for observed rows -- an absent dimension, not missing data.

    The two occupancy columns are deliberately NOT collapsed into one. They are
    disjoint in practice (observed sets occupancy_status and never
    departure_occupancy_status; predicted does the reverse), so one column plus
    `origin` would be lossless -- but they are different measurements, and
    collapsing them would make `origin` load-bearing for meaning rather than
    provenance.
    """

    feed_timestamp: int | None = None
    origin: str | None = None
    vehicle_id: str | None = None
    trip_id: str | None = None
    stop_sequence: int | None = None
    stop_id: str | None = None
    # NB: `required` in the proto, but Metro reports 0 for every carriage and
    # conveys ordering via `name`. Do not use for cross-agency carriage order.
    position_in_consist: int | None = None
    name: str | None = None
    occupancy_status: str | None = None
    departure_occupancy_status: str | None = None
    quiet_carriage: bool | None = None
    toilet: str | None = None
    luggage_rack: bool | None = None


@Decoder.register("tfnsw")
class TfnswDecoder(GtfsRtDecoder):
    """Four tables: the usual three plus `consist`.

    TODO: also add "tfnsw" to the `decoder` Literal in archiver/config.py, or
    config validation will reject any feed that names it.

    OPEN DESIGN QUESTION -- worth settling before filling in the _decode_* hooks
    below. rollup.py dispatches to the Rust decoder on
    `type(feed.decoder) is StandardDecoder`, so a TfnswDecoder falls into the
    *Python* branch, which would need TfNSW protobuf _pb2 bindings that this
    repo does not generate. Two ways out:

      (a) Widen the rust-dispatch check to cover this class too. Then the three
          _decode_* hooks below are never called, and exist only to satisfy the
          GtfsRtDecoder ABC -- which is a smell worth naming: `produces` is a
          *schema declaration*, not decoding behaviour. It has lived on the
          decoder only because until now every decoder decoded in Python.

      (b) Generate TfNSW _pb2 bindings in the build and implement the hooks for
          real, keeping a working Python fallback.

    (a) is less code; (b) keeps the abstraction honest. Deciding this is the
    point of the exercise -- don't route around it by implementing the hooks as
    `raise NotImplementedError` and moving on.
    """

    produces: ClassVar[dict[type[Row], TableSpec]] = {
        # TODO: VehicleRow / StopTimeUpdateRow specs -- start from
        # StandardDecoder.produces and drop what TfNSW does not publish
        # (occupancy_percentage, multi_carriage_details), then add the
        # TfNSW-only columns (vehicle_model, wheelchair_accessible,
        # track_direction, and StopTimeUpdate.departure_occupancy_status).
        # Decide whether these reuse VehicleRow/StopTimeUpdateRow or need
        # TfNSW-specific row classes -- reuse means adding nullable columns
        # that every other agency leaves null.
        VehicleRow: TableSpec(
            "vehicles", dedup_keys=("vehicle_id", "vehicle_timestamp")
        ),
        StopTimeUpdateRow: TableSpec("trip_updates"),
        AlertRow: TableSpec("alerts"),
        ConsistRow: TableSpec(
            "consist",
            # TODO: what makes a carriage row unique? Candidate:
            # (vehicle_id, feed_timestamp, origin, position_in_consist) -- but
            # position_in_consist is 0 for all Metro carriages, so that key
            # collapses there. `name` disambiguates on Metro but is absent on
            # Sydney Trains. Worth checking against real data before choosing.
            dedup_keys=(),
        ),
    }
    rust_decode: ClassVar[str | None] = "decode_arrow_tfnsw"

    def _decode_vehicle(self, vp, header, fetched_at: int | None = None) -> VehicleRow:
        raise NotImplementedError("see OPEN DESIGN QUESTION in the class docstring")

    def _decode_trip_update(self, tu, header, fetched_at: int | None = None):
        raise NotImplementedError("see OPEN DESIGN QUESTION in the class docstring")

    def _decode_alert(self, sa, alert_id, header, fetched_at: int | None = None):
        raise NotImplementedError("see OPEN DESIGN QUESTION in the class docstring")
