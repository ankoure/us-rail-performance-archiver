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
    StandardDecoder,
    StopTimeUpdateRow,
    TableSpec,
    VehicleRow,
)

# Mirrored, not retyped: TfNSW writes to the same `vehicles` / `trip_updates`
# tables as every other feed, and analysis/vehicle_day.py and
# analysis/trip_updates_day.py read those by the dotted parquet names. Spreading
# the canonical maps keeps them in lockstep if the canonical ones ever change.
_STD_VEHICLE = StandardDecoder.produces[VehicleRow]
_STD_STU = StandardDecoder.produces[StopTimeUpdateRow]


@dataclass
class TfnswVehicleRow(VehicleRow):
    """VehiclePosition row plus the TfNSW 1007 extension fields.

    Subclasses rather than replaces VehicleRow: every inherited field already
    has a default, so the four additions cost four lines. Column *order* is
    irrelevant -- rollup's _batch_to_parquet_table looks columns up by name --
    so the fact that inheritance appends these at the end doesn't matter.

    `occupancy_percentage` comes along inherited and is written all-null, since
    TfNSW does not publish it. That is deliberate: analysis/event_export.py
    names the column, so dropping it would break a downstream reader for the
    sake of a column that is already null on these feeds today.
    """

    vehicle_model: str | None = None
    air_conditioned: bool | None = None
    wheelchair_accessible: int | None = None
    track_direction: str | None = None


@dataclass
class TfnswStopTimeUpdateRow(StopTimeUpdateRow):
    """StopTimeUpdate row plus TfNSW's field-6 departure_occupancy_status.

    This single extra column is the reason TfNSW is modelled as its own
    permutation rather than an extension. Canonical GTFS-RT uses field 6 for
    `stop_time_properties` (a submessage) and puts departure_occupancy_status
    at 7; TfNSW puts it at 6. The wire types differ, so the canonical decoder
    cannot read it at all -- measured at ~1919 of 4439 stop time updates on a
    live Sydney Trains payload.
    """

    departure_occupancy_status: str | None = None


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

    rust_decode: ClassVar[str | None] = "decode_arrow_tfnsw"

    produces: ClassVar[dict[type[Row], TableSpec]] = {
        TfnswVehicleRow: TableSpec(
            "vehicles",
            dedup_keys=("vehicle_id", "vehicle_timestamp"),
            column_names={
                **_STD_VEHICLE.column_names,
                "vehicle_model": "vehicle.vehicle.tfnsw_vehicle_descriptor.vehicle_model",
                "air_conditioned": "vehicle.vehicle.tfnsw_vehicle_descriptor.air_conditioned",
                "wheelchair_accessible": "vehicle.vehicle.tfnsw_vehicle_descriptor.wheelchair_accessible",
                "track_direction": "vehicle.position.track_direction",
            },
            # Mirrored from StandardDecoder so a TfNSW `vehicles` file has the
            # same shape as every other agency's. Note vehicle.vehicle.consist
            # and vehicle.multi_carriage_details stay all-null here -- TfNSW's
            # real carriage data lives in the `consist` table below, which has a
            # grain these list columns cannot express.
            extra_columns=_STD_VEHICLE.extra_columns,
        ),
        TfnswStopTimeUpdateRow: TableSpec(
            "trip_updates",
            column_names={
                **_STD_STU.column_names,
                "departure_occupancy_status": "trip_update.stop_time_update.departure_occupancy_status",
            },
        ),
        AlertRow: TableSpec("alerts"),
        ConsistRow: TableSpec(
            "consist",
            # Verified unique across vehiclepos-sydneytrains (1883 rows),
            # vehiclepos-metro (174), realtime-sydneytrains (15020) and
            # newcastle light rail (3). Every component earns its place:
            #   * position_in_consist alone collapses on Metro, which reports 0
            #     for every carriage and conveys order via `name`.
            #   * `name` alone collapses on Sydney Trains, which never sets it.
            #   * vehicle_id is null on the whole predicted stream (TfNSW trip
            #     updates carry a VehicleDescriptor with no id), so trip_id and
            #     stop_id are what separate those rows.
            #   * stop_sequence is null in every feed sampled so far, but costs
            #     nothing and covers feeds that do send it.
            dedup_keys=(
                "feed_timestamp",
                "origin",
                "vehicle_id",
                "trip_id",
                "stop_id",
                "stop_sequence",
                "position_in_consist",
                "name",
            ),
        ),
    }

    def _decode_vehicle(self, vp, header, fetched_at: int | None = None) -> VehicleRow:
        raise NotImplementedError("see OPEN DESIGN QUESTION in the class docstring")

    def _decode_trip_update(self, tu, header, fetched_at: int | None = None):
        raise NotImplementedError("see OPEN DESIGN QUESTION in the class docstring")

    def _decode_alert(self, sa, alert_id, header, fetched_at: int | None = None):
        raise NotImplementedError("see OPEN DESIGN QUESTION in the class docstring")
