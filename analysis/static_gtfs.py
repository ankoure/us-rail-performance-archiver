"""Load a GTFS static zip and answer scheduled_tt / scheduled_headway queries.

The matching semantics follow TransitMatters' gobble (gtfs.py:add_gtfs_headways):

  scheduled_tt        = seconds elapsed from the trip's first scheduled
                        arrival to its arrival at this stop.
  scheduled_headway   = seconds between consecutive scheduled arrivals at
                        (route_id, direction_id, stop_id) on the same
                        service day.

Service-day filtering combines calendar.txt and calendar_dates.txt: a
service_id is active on a date iff (a) it falls within [start_date, end_date]
and the weekday flag is 1, **and** (b) calendar_dates.txt has no exception_type
== 2 ('removed') for that (service_id, date), and additionally any service_id
present in calendar_dates with exception_type == 1 ('added') on that date is
included.

The class is intentionally focused on what events.csv enrichment needs; it
is not a general GTFS query library.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from functools import cached_property, lru_cache
from pathlib import Path

import pandas as pd

# GTFS calendar.txt has one column per weekday, named lower-case English.
_WEEKDAY_COLS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
_RTE_DIR_STOP = ["route_id", "direction_id", "stop_id"]


class StaticGtfs:
    """One GTFS static zip, read lazily and answering enrichment queries.

    Each component table (`trips`, `routes`, `stop_times`, `calendar`,
    `calendar_dates`) is read from the zip on first access and cached. Missing
    optional files degrade to empty frames rather than raising, so a feed that
    omits e.g. calendar_dates.txt still works. The zip is never extracted to
    disk — tables are read straight out of the archive.
    """

    def __init__(self, zip_path: Path | str) -> None:
        self.zip_path = Path(zip_path)
        if not self.zip_path.exists():
            raise FileNotFoundError(self.zip_path)

    def __repr__(self) -> str:
        return f"StaticGtfs({self.zip_path})"

    def _read(self, name: str, **read_csv_kwargs) -> pd.DataFrame:
        # skipinitialspace handles agencies (e.g. Metra) whose CSV headers
        # have a space after each delimiter — without it, columns end up
        # named " trip_id" instead of "trip_id".
        read_csv_kwargs.setdefault("skipinitialspace", True)
        with zipfile.ZipFile(self.zip_path) as z:
            with z.open(name) as f:
                return pd.read_csv(f, **read_csv_kwargs)

    @cached_property
    def trips(self) -> pd.DataFrame:
        return self._read(
            "trips.txt",
            usecols=lambda c: (
                c
                in {
                    "trip_id",
                    "route_id",
                    "service_id",
                    "direction_id",
                    "trip_short_name",
                }
            ),
            dtype={
                "trip_id": str,
                "route_id": str,
                "service_id": str,
                "trip_short_name": str,
            },
        )

    @cached_property
    def routes(self) -> pd.DataFrame:
        try:
            return self._read(
                "routes.txt",
                usecols=lambda c: (
                    c
                    in {"route_id", "route_type", "route_short_name", "route_long_name"}
                ),
                dtype={
                    "route_id": str,
                    "route_short_name": str,
                    "route_long_name": str,
                },
            )
        except KeyError:
            return pd.DataFrame(
                columns=[
                    "route_id",
                    "route_type",
                    "route_short_name",
                    "route_long_name",
                ]
            )

    @cached_property
    def stops(self) -> pd.DataFrame:
        try:
            return self._read(
                "stops.txt",
                usecols=lambda c: (
                    c in {"stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"}
                ),
                dtype={"stop_id": str, "stop_code": str, "stop_name": str},
            )
        except KeyError:
            return pd.DataFrame(
                columns=["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"]
            )

    @cached_property
    def stop_coords(self) -> dict[str, tuple[float, float]]:
        """Map stop_id → (latitude, longitude) from stops.txt.

        Stops with missing or non-finite coordinates are omitted. Used by the
        segment-speed mart to compute haversine distances between consecutive stops.
        """
        df = self.stops
        if "stop_lat" not in df.columns or "stop_lon" not in df.columns:
            return {}
        out: dict[str, tuple[float, float]] = {}
        for row in df.itertuples(index=False):
            sid = getattr(row, "stop_id", None)
            if not isinstance(sid, str) or not sid:
                continue
            lat_raw = getattr(row, "stop_lat", None)
            lon_raw = getattr(row, "stop_lon", None)
            if not (pd.notna(lat_raw) and pd.notna(lon_raw)):
                continue
            try:
                lat, lon = float(lat_raw), float(lon_raw)
            except (TypeError, ValueError):
                continue
            out[sid] = (lat, lon)
        return out

    @cached_property
    def route_modes(self) -> dict[str, str]:
        """Map route_id → mode bucket: 'rapid', 'bus', 'cr', or 'other'.

        Reads route_type from routes.txt and applies [[_categorize_route_type]],
        which accepts both the base GTFS values (0..12) and Google's extended
        Hierarchical Vehicle Types (100..1799). Used by the event exporter to
        partition output folders by mode without the caller having to know any
        GTFS route_type values.
        """
        df = self.routes
        if "route_id" not in df.columns or "route_type" not in df.columns:
            return {}
        out: dict[str, str] = {}
        for row in df.itertuples(index=False):
            rid = getattr(row, "route_id", None)
            if not isinstance(rid, str):
                continue
            raw = getattr(row, "route_type", None)
            try:
                rt: int | None = int(raw) if pd.notna(raw) else None
            except (TypeError, ValueError):
                rt = None
            out[rid] = _categorize_route_type(rt)
        return out

    @cached_property
    def trip_directions(self) -> dict[str, tuple[str | None, int | None]]:
        """Map trip_id → (route_id, direction_id) from trips.txt.

        Used to backfill the trip descriptor for GTFS-rt feeds (NYCT subway,
        TriMet, several CARTA/UTA/PRT feeds) that publish trip_id but omit
        direction_id — and sometimes route_id — in their realtime payload.

        Returns an empty map when the static feed's trips.txt is missing
        trip_id (some agencies publish a schema that drops required columns,
        or wraps headers in whitespace/BOM that breaks the column name).
        """
        if "trip_id" not in self.trips.columns:
            return {}
        out: dict[str, tuple[str | None, int | None]] = {}
        has_dir = "direction_id" in self.trips.columns
        has_route = "route_id" in self.trips.columns
        for row in self.trips.itertuples(index=False):
            trip_id = getattr(row, "trip_id", None)
            if not isinstance(trip_id, str):
                continue
            route_id = getattr(row, "route_id", None) if has_route else None
            if not isinstance(route_id, str):
                route_id = None
            direction_id: int | None = None
            if has_dir:
                raw = getattr(row, "direction_id", None)
                if pd.notna(raw):
                    direction_id = int(raw)
            out[trip_id] = (route_id, direction_id)
        return out

    # ------------------------------------------------------------------
    # Resolution indexes — raw realtime identifier -> canonical GTFS id.
    # Ported from nibble's StaticGTFS (keep-first semantics): they let the
    # gold-layer normalizers resolve the raw tokens our JSON feeds land
    # (route short/long names, stop codes/names, train numbers).
    # ------------------------------------------------------------------

    @staticmethod
    def _keepfirst_index(
        df: pd.DataFrame,
        value_col: str,
        key_cols: tuple[str, ...],
        *,
        key_transform=None,
    ) -> dict[str, str]:
        """Build a keep-first ``{stripped key -> value}`` map from a GTFS table.

        Key columns are scanned in order, so an earlier column's keys take
        precedence over a later one's on collision (e.g. route_short_name beats
        route_long_name) regardless of row order. A row is kept only when both
        the value and the key are non-empty strings; ``key_transform`` (e.g.
        ``str.upper``) is applied to the stripped key. Returns {} when
        ``value_col`` is absent.
        """
        if value_col not in df.columns:
            return {}
        out: dict[str, str] = {}
        for col in key_cols:
            if col not in df.columns:
                continue
            for value, key in zip(df[value_col], df[col]):
                if not isinstance(value, str) or not value:
                    continue
                if isinstance(key, str) and key.strip():
                    k = key.strip()
                    out.setdefault(key_transform(k) if key_transform else k, value)
        return out

    @cached_property
    def route_short_names(self) -> dict[str, str]:
        """Map route_short_name AND route_long_name -> route_id.

        Indexing both names lets feeds that report either as their route
        identifier (MWRTA short codes, CCRTA/Passio long names, VTA headsigns)
        resolve through one dict. Precedence is independent of routes.txt row
        order: short names are indexed before long names, so a long name can
        never shadow another route's short name. Within a single tier, ties keep
        the first route in file order.
        """
        return self._keepfirst_index(
            self.routes, "route_id", ("route_short_name", "route_long_name")
        )

    @cached_property
    def stop_codes(self) -> dict[str, str]:
        """Map stop_code -> stop_id from stops.txt (LIRR-style raw codes)."""
        return self._keepfirst_index(self.stops, "stop_id", ("stop_code",))

    @cached_property
    def stop_names(self) -> dict[str, str]:
        """Map UPPER(stop_name) -> stop_id (MARTA station-name precedent)."""
        return self._keepfirst_index(
            self.stops, "stop_id", ("stop_name",), key_transform=str.upper
        )

    @cached_property
    def trip_short_names(self) -> dict[str, str]:
        """Map trip_short_name -> trip_id from trips.txt (LIRR train numbers)."""
        return self._keepfirst_index(self.trips, "trip_id", ("trip_short_name",))

    @cached_property
    def route_ids(self) -> set[str]:
        """Set of known GTFS route_ids — used to detect tokens that are already canonical."""
        df = self.routes
        if "route_id" not in df.columns:
            return set()
        return {r for r in df["route_id"] if isinstance(r, str) and r}

    @cached_property
    def stop_times(self) -> pd.DataFrame:
        """stop_times.txt plus derived `arrival_seconds` / `departure_seconds`.

        The HH:MM:SS time strings (which GTFS allows past 24:00 for next-day
        continuations) are converted to seconds via [[_hms_to_seconds]] so
        scheduled_tt and scheduled_headway are plain integer subtractions.
        """
        df = self._read(
            "stop_times.txt",
            usecols=lambda c: (
                c
                in {
                    "trip_id",
                    "stop_sequence",
                    "stop_id",
                    "arrival_time",
                    "departure_time",
                }
            ),
            dtype={"trip_id": str, "stop_id": str},
        )
        # GTFS allows times like "25:30:00" (next-day continuation). Convert to seconds-since-noon-of-service-date
        # following the GTFS convention (noon-12h is the practical reference; using midnight is fine for our use
        # since both event and schedule are subtracted from the same midnight).
        df["arrival_seconds"] = df["arrival_time"].map(_hms_to_seconds)
        df["departure_seconds"] = df["departure_time"].map(_hms_to_seconds)
        return df

    @cached_property
    def calendar(self) -> pd.DataFrame:
        try:
            df = self._read(
                "calendar.txt",
                dtype={"service_id": str},
                parse_dates=["start_date", "end_date"],
                date_format="%Y%m%d",
            )
        except KeyError:
            return pd.DataFrame(
                columns=["service_id", *_WEEKDAY_COLS, "start_date", "end_date"]
            )
        return df

    @cached_property
    def calendar_dates(self) -> pd.DataFrame:
        try:
            df = self._read(
                "calendar_dates.txt",
                dtype={"service_id": str},
                parse_dates=["date"],
                date_format="%Y%m%d",
            )
        except KeyError:
            return pd.DataFrame(columns=["service_id", "date", "exception_type"])
        return df

    def active_service_ids(self, service_date: dt.date) -> set[str]:
        """Service_ids active on the given local service date."""
        active: set[str] = set()
        target = pd.Timestamp(service_date)
        weekday_col = _WEEKDAY_COLS[service_date.weekday()]
        if not self.calendar.empty:
            cal = self.calendar
            mask = (
                (cal[weekday_col] == 1)
                & (cal["start_date"] <= target)
                & (cal["end_date"] >= target)
            )
            active.update(cal.loc[mask, "service_id"])
        if not self.calendar_dates.empty:
            cd = self.calendar_dates
            on_date = cd[cd["date"] == target]
            added = on_date[on_date["exception_type"] == 1]["service_id"]
            removed = on_date[on_date["exception_type"] == 2]["service_id"]
            active.update(added)
            active.difference_update(removed)
        return active

    @lru_cache(maxsize=8)
    def scheduled_stops(self, service_date: dt.date) -> pd.DataFrame:
        """Joined stop_times × trips for one service day, with scheduled_tt and scheduled_headway."""
        empty_cols = [
            "trip_id",
            "stop_sequence",
            "stop_id",
            "arrival_time",
            "departure_time",
            "arrival_seconds",
            "departure_seconds",
            "route_id",
            "direction_id",
            "scheduled_tt",
            "scheduled_headway",
        ]
        services = self.active_service_ids(service_date)
        if not services:
            return pd.DataFrame(columns=empty_cols)
        trips_today = self.trips[self.trips["service_id"].isin(services)]
        joined = self.stop_times.merge(
            trips_today[["trip_id", "route_id", "direction_id"]],
            on="trip_id",
            how="inner",
        )
        if joined.empty:
            return pd.DataFrame(columns=empty_cols)

        # scheduled_tt: arrival_seconds minus the trip's first-stop arrival_seconds.
        first_arrival = joined.groupby("trip_id")["arrival_seconds"].transform("min")
        joined["scheduled_tt"] = (joined["arrival_seconds"] - first_arrival).astype(
            "Int64"
        )

        # scheduled_headway: per (route, dir, stop), diff between consecutive scheduled arrivals.
        joined = joined.sort_values(_RTE_DIR_STOP + ["arrival_seconds"]).reset_index(
            drop=True
        )
        joined["scheduled_headway"] = (
            joined.groupby(_RTE_DIR_STOP)["arrival_seconds"].diff().astype("Int64")
        )
        return joined

    def enrich_events(
        self,
        events: list[dict],
        service_date: dt.date,
    ) -> None:
        """Populate `scheduled_headway` and `scheduled_tt` on each event dict in-place.

        `events` must all share the same service_date (caller buckets first).
        Each event dict must have at minimum: trip_id, stop_id. Both fields are
        properties of the scheduled trip at that stop, so the lookup is a direct
        merge on (trip_id, stop_id) — using the actual event time would attribute
        a neighboring trip's values whenever the vehicle ran early or late.
        """
        if not events:
            return
        sched = self.scheduled_stops(service_date)
        if sched.empty:
            return

        lookup = (
            sched[["trip_id", "stop_id", "scheduled_tt", "scheduled_headway"]]
            .drop_duplicates(["trip_id", "stop_id"], keep="first")
            .set_index(["trip_id", "stop_id"])
            .to_dict("index")
        )
        for ev in events:
            row = lookup.get((ev.get("trip_id"), ev.get("stop_id")))
            if row is None:
                continue
            tt = row["scheduled_tt"]
            hw = row["scheduled_headway"]
            ev["scheduled_tt"] = "" if pd.isna(tt) else int(tt)
            ev["scheduled_headway"] = "" if pd.isna(hw) else int(hw)


_BASE_ROUTE_TYPES: dict[int, str] = {
    0: "rapid",  # Tram, Streetcar, Light rail
    1: "rapid",  # Subway, Metro
    2: "cr",  # Rail (intercity / commuter)
    3: "bus",
    5: "rapid",  # Cable tram
    11: "bus",  # Trolleybus
    12: "rapid",  # Monorail
}


def _categorize_route_type(rt: int | None) -> str:
    """Bucket a GTFS route_type into 'rapid', 'bus', 'cr', or 'other'.

    Recognizes both base GTFS route_types and Google's extended Hierarchical
    Vehicle Type ranges (RouteType reference at
    developers.google.com/transit/gtfs/reference/extended-route-types):
      100..117  Railway service (intercity / regional) → cr
      200..209  Coach service                          → bus
      300..307  Suburban Railway                       → cr
      400..405  Urban Railway / Metro                  → rapid
      700..716  Bus service                            → bus
      800       Trolleybus                             → bus
      900..906  Tram service                           → rapid
    Anything else (ferry, aerial lift, taxi, ...) falls into 'other'.
    """
    if rt is None:
        return "other"
    base = _BASE_ROUTE_TYPES.get(rt)
    if base is not None:
        return base
    if 100 <= rt <= 117 or 300 <= rt <= 307:
        return "cr"
    if 200 <= rt <= 209 or 700 <= rt <= 716 or rt == 800:
        return "bus"
    if 400 <= rt <= 405 or 900 <= rt <= 906:
        return "rapid"
    return "other"


def _hms_to_seconds(s: str) -> int:
    """Parse 'H:MM:SS' or 'HH:MM:SS' (allows hours >= 24) to total seconds.

    Returns -1 for missing/invalid values so the column stays int.
    """
    if not isinstance(s, str):
        return -1
    parts = s.split(":")
    if len(parts) != 3:
        return -1
    try:
        h, m, sec = (int(p) for p in parts)
    except ValueError:
        return -1
    return h * 3600 + m * 60 + sec
