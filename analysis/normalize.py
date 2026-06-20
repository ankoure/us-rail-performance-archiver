"""Normalize raw JSON-feed curated rows into a canonical vehicle-position table.

This is the gold-layer port of nibble's normalization: it resolves the *raw*
identifiers our JSON decoders landed (route short/long names, stop codes, train
numbers, Swiv idLigne, Passio route display names) to canonical GTFS ids using
the resolution indexes on [[analysis.static_gtfs.StaticGtfs]], and standardizes
units (mph/km-h -> m/s) and time (naive-local / ISO-offset -> UTC unix).

Scope is ID-resolution + unit/time only. Position-based trip/stop inference is
deliberately out of scope for this pass, so feeds that carry neither a stop code
nor a resolvable trip number land with ``stop_id``/``trip_id`` = None.

One ``Normalizer`` is registered per *curated table name* (mirroring the
``Decoder`` registry in archiver/decoder.py). Several feeds share a table
(mwrta_vehicles = MWRTA+CCRTA, swiv_vehicles = GATRA/LRTA/WRTA,
passio_vehicles = FRTA+MART); per-agency divergence is handled with the
``agency_id`` argument, not separate classes.
"""

from __future__ import annotations

import datetime as dt
import re
from abc import ABC, abstractmethod
from typing import ClassVar
from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.static_gtfs import StaticGtfs

# Canonical normalized vehicle-position schema. The partition keys (feed/year/
# month/day) live in the path and are NOT stored as columns — storing `feed`
# in-file as well would collide with the dictionary-typed partition key on a
# dataset read (same convention as the adherence/metrics marts). Rows still
# carry `feed` in memory; from_pylist drops it against this schema on write.
NORMALIZED_VEHICLES_SCHEMA = pa.schema(
    [
        pa.field("vehicle_id", pa.string(), nullable=False),
        pa.field("ts_utc", pa.int64(), nullable=True),  # unix seconds, UTC
        pa.field("latitude", pa.float64(), nullable=True),
        pa.field("longitude", pa.float64(), nullable=True),
        pa.field("route_id", pa.string(), nullable=True),  # resolved GTFS route_id
        pa.field("trip_id", pa.string(), nullable=True),  # resolved GTFS trip_id
        pa.field("stop_id", pa.string(), nullable=True),  # resolved GTFS stop_id
        pa.field("speed_mps", pa.float64(), nullable=True),
        pa.field("bearing", pa.float64(), nullable=True),
        # provenance
        pa.field("raw_route", pa.string(), nullable=True),  # token we resolved from
        pa.field("raw_speed", pa.float64(), nullable=True),  # kept when units unknown
        pa.field("speed_unit", pa.string(), nullable=True),  # 'mps' | 'unknown'
        pa.field("resolution_status", pa.string(), nullable=False),
    ]
)

_CANONICAL_COLUMNS = [f.name for f in NORMALIZED_VEHICLES_SCHEMA]

# resolution_status vocabulary
_RESOLVED = "resolved"  # raw token resolved to a GTFS route_id
_UNRESOLVED = "route_unresolved"  # raw token present but no GTFS match
_NO_ROUTE = "no_route"  # feed carried no route hint
_PASSTHROUGH = "passthrough"  # raw token already a GTFS route_id

MPH_TO_MPS = 0.44704


# ---------------------------------------------------------------------------
# Pure helpers (unit / time / token resolution) — unit-tested directly.
# ---------------------------------------------------------------------------


def _is_missing(v) -> bool:
    return v is None or (isinstance(v, float) and v != v)  # None or NaN


def mph_to_mps(v) -> float | None:
    if _is_missing(v):
        return None
    try:
        return float(v) * MPH_TO_MPS
    except (TypeError, ValueError):
        return None


def kmh_to_mps(v) -> float | None:
    if _is_missing(v):
        return None
    try:
        return float(v) / 3.6
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    if _is_missing(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def naive_local_to_utc(
    iso: str | None, tz: ZoneInfo, reference_unix: int | None = None
) -> int | None:
    """Parse a naive-local ISO string (no offset) and return UTC unix seconds.

    During the DST fall-back overlap a wall clock like ``01:30`` maps to two UTC
    instants an hour apart, and the feed carries no offset to tell them apart.
    When ``reference_unix`` (the poll time — an unambiguous UTC anchor the
    vehicle timestamp is always near) is given, we pick whichever candidate is
    closest to it; otherwise we fall back to ``fold=0`` (the earlier instant).
    """
    if not isinstance(iso, str) or not iso.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return int(parsed.astimezone(dt.timezone.utc).timestamp())
    parsed = parsed.replace(tzinfo=tz)
    earlier = int(parsed.astimezone(dt.timezone.utc).timestamp())
    if reference_unix is None:
        return earlier
    later = int(parsed.replace(fold=1).astimezone(dt.timezone.utc).timestamp())
    if earlier == later:  # unambiguous wall clock — fold is a no-op
        return earlier
    return min((earlier, later), key=lambda u: abs(u - reference_unix))


def iso_offset_to_utc(iso: str | None) -> int | None:
    """Parse an ISO string that carries an offset (or trailing 'Z') to UTC unix."""
    if not isinstance(iso, str) or not iso.strip():
        return None
    s = iso.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # offset feed but no offset present — caller shouldn't reach here
    return int(parsed.astimezone(dt.timezone.utc).timestamp())


def combine_clock_with_poll_date(
    clock: str | None, feed_timestamp: int | None, tz: ZoneInfo
) -> int | None:
    """Combine a coarse "03:41 PM" clock string with the poll date.

    Passio reports only a minute-granular wall-clock with no date. We anchor it
    to the local date of the poll (feed_timestamp), rolling back a day if the
    combined time lands more than ~12h *after* the poll (i.e. it was actually
    the previous day, near midnight). Result is UTC unix, accurate to ±1 minute.
    """
    if not isinstance(clock, str) or not clock.strip() or feed_timestamp is None:
        return feed_timestamp
    try:
        t = dt.datetime.strptime(clock.strip(), "%I:%M %p").time()
    except ValueError:
        return feed_timestamp
    poll_local = dt.datetime.fromtimestamp(feed_timestamp, tz)
    candidate = dt.datetime.combine(poll_local.date(), t, tzinfo=tz)
    if (candidate - poll_local).total_seconds() > 12 * 3600:
        candidate -= dt.timedelta(days=1)
    return int(candidate.astimezone(dt.timezone.utc).timestamp())


# Route-token resolution. Each returns (route_id | None, resolution_status).


def resolve_route_exact(raw, gtfs: StaticGtfs) -> tuple[str | None, str]:
    """Exact match against route_short_names, with passthrough for real route_ids."""
    if _is_missing(raw):
        return None, _NO_ROUTE
    token = str(raw).strip()
    if not token:
        return None, _NO_ROUTE
    if token in gtfs.route_ids:
        return token, _PASSTHROUGH
    rid = gtfs.route_short_names.get(token)
    if rid:
        return rid, _RESOLVED
    return None, _UNRESOLVED


def resolve_route_ccrta(raw, gtfs: StaticGtfs) -> tuple[str | None, str]:
    """CCRTA: exact, then case-insensitive prefix/substring against route names.

    The fuzzy fallback can match several names (a short token is a substring of
    many), so rather than return whichever happens to be first in dict order we
    rank candidates by match tightness — a prefix beats a bare substring, and a
    shorter name beats a longer one (less padding around the token) — and pick
    the best. Resolution is then deterministic and lands on the closest name.
    """
    rid, status = resolve_route_exact(raw, gtfs)
    if rid is not None:
        return rid, status
    if _is_missing(raw):
        return None, _NO_ROUTE
    lower = str(raw).strip().lower()
    if not lower:
        return None, _NO_ROUTE
    best_key: tuple[int, int, str] | None = None  # (rank, name_len, name); lower wins
    best_id: str | None = None
    for name, route_id in gtfs.route_short_names.items():
        nl = name.lower()
        if nl.startswith(lower):
            rank = 0
        elif lower in nl:
            rank = 1
        else:
            continue
        key = (rank, len(nl), nl)
        if best_key is None or key < best_key:
            best_key, best_id = key, route_id
    if best_id is not None:
        return best_id, _RESOLVED
    return None, _UNRESOLVED


_PASSIO_DIRECTION_SUFFIXES = (" South", " North", " East", " West", " Loop")


def resolve_route_passio(raw, gtfs: StaticGtfs) -> tuple[str | None, str]:
    """Passio: exact display name, strip 'Route ' prefix, strip directional suffix."""
    rid, status = resolve_route_exact(raw, gtfs)
    if rid is not None:
        return rid, status
    if _is_missing(raw):
        return None, _NO_ROUTE
    token = str(raw).strip()
    if token.lower().startswith("route "):
        rid = gtfs.route_short_names.get(token[len("route ") :].strip())
        if rid:
            return rid, _RESOLVED
    for suffix in _PASSIO_DIRECTION_SUFFIXES:
        if token.endswith(suffix):
            rid = gtfs.route_short_names.get(token[: -len(suffix)].strip())
            if rid:
                return rid, _RESOLVED
    return None, _UNRESOLVED


_BRTA_PREFIX_RE = re.compile(r"^(?:Wk\s+)?(?:Rte?\.?\s+|Route\s+)", re.IGNORECASE)
_BRTA_NUMBER_RE = re.compile(r"^(\d+[A-Za-z]?)")


def brta_candidate_short_name(route_id: str | None) -> str | None:
    """Extract a GTFS route_short_name from a RouteMatch masterRouteId.

    "Wk Rt 01" -> "1", "Rte 34" -> "34", "Route 5 Loop" -> "5".
    """
    if not isinstance(route_id, str):
        return None
    stripped = _BRTA_PREFIX_RE.sub("", route_id).strip()
    m = _BRTA_NUMBER_RE.match(stripped)
    if not m:
        return None
    token = m.group(1)
    digits = token.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    suffix = token[len(digits) :].upper()
    try:
        return str(int(digits)) + suffix
    except ValueError:
        return None


# WRTA static fallback: Swiv idLigne -> GTFS route_short_name (ported from
# nibble/normalizer/wrta.py). Resolved through route_short_names afterwards.
_WRTA_LIGNE_TO_SHORT_NAME: dict[str, str] = {
    "18045": "27",
    "18046": "2",
    "18047": "4",
    "18062": "30",
    "18063": "33",
    "18068": "1",
    "18069": "11",
    "18070": "7",
}


# ---------------------------------------------------------------------------
# Normalizer registry
# ---------------------------------------------------------------------------


class Normalizer(ABC):
    """Base class; subclasses register against a curated table name."""

    _registry: ClassVar[dict[str, type["Normalizer"]]] = {}

    # Whether this normalizer resolves routes through a topo sidecar table
    # (idLigne -> nomCommercial) that the driver must load and pass as
    # ``topo_map``. Declared here so the driver stays table-agnostic — it asks
    # the normalizer rather than hardcoding which table needs the lookup.
    needs_topo: ClassVar[bool] = False

    @classmethod
    def register(cls, table: str):
        def decorator(subclass: type["Normalizer"]) -> type["Normalizer"]:
            if table in cls._registry:
                raise ValueError(
                    f"normalizer table {table!r} already registered to "
                    f"{cls._registry[table].__name__}"
                )
            cls._registry[table] = subclass
            return subclass

        return decorator

    @classmethod
    def from_table(cls, table: str) -> "Normalizer":
        try:
            return cls._registry[table]()
        except KeyError:
            raise KeyError(
                f"no normalizer registered for table {table!r}; "
                f"known: {sorted(cls._registry)}"
            ) from None

    @abstractmethod
    def normalize(
        self,
        rows: list[dict],
        gtfs: StaticGtfs,
        *,
        feed: str,
        agency_id: str,
        agency_tz: ZoneInfo,
        topo_map: dict[str, str] | None = None,
    ) -> list[dict]:
        """Return canonical rows (one per input row) conforming to the schema."""
        raise NotImplementedError

    @staticmethod
    def _base(feed: str, vehicle_id, *, lat=None, lon=None, bearing=None) -> dict:
        """A canonical row with every field defaulted; normalizers fill the rest."""
        return {
            "feed": feed,
            "vehicle_id": "" if vehicle_id is None else str(vehicle_id),
            "ts_utc": None,
            "latitude": _to_float(lat),
            "longitude": _to_float(lon),
            "route_id": None,
            "trip_id": None,
            "stop_id": None,
            "speed_mps": None,
            "bearing": _to_float(bearing),
            "raw_route": None,
            "raw_speed": None,
            "speed_unit": None,
            "resolution_status": _NO_ROUTE,
        }


def _str_or_none(v) -> str | None:
    if _is_missing(v):
        return None
    s = str(v).strip()
    return s or None


@Normalizer.register("lirr_trains")
class LirrNormalizer(Normalizer):
    """LIRR: train_num -> trip_id (trip_short_names); stop code -> stop_id; mph -> m/s."""

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        out = []
        for r in rows:
            row = self._base(
                feed, r.get("train_num"), lat=r.get("latitude"), lon=r.get("longitude")
            )
            row["resolution_status"] = _NO_ROUTE  # LIRR carries no route token
            train_num = _str_or_none(r.get("train_num"))
            if train_num:
                row["trip_id"] = gtfs.trip_short_names.get(train_num)
            code = _str_or_none(r.get("current_stop_code"))
            if code:
                row["stop_id"] = gtfs.stop_codes.get(code)
            row["speed_mps"] = mph_to_mps(r.get("speed_mph"))
            row["speed_unit"] = "mps"
            ts = r.get("location_timestamp")
            row["ts_utc"] = int(ts) if not _is_missing(ts) else r.get("feed_timestamp")
            out.append(row)
        return out


@Normalizer.register("mwrta_vehicles")
class MwrtaNormalizer(Normalizer):
    """MWRTA/CCRTA: route name -> route_id; naive-local DateTime -> UTC; speed raw (unknown units)."""

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        ccrta = agency_id.upper() == "CCRTA"
        out = []
        for r in rows:
            row = self._base(
                feed,
                r.get("vehicle_id"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                bearing=r.get("heading"),
            )
            raw_route = r.get("route_name") if ccrta else r.get("route")
            if _str_or_none(raw_route) is None:  # CCRTA RouteName absent -> fall back
                raw_route = r.get("route")
            row["raw_route"] = _str_or_none(raw_route)
            resolver = resolve_route_ccrta if ccrta else resolve_route_exact
            row["route_id"], row["resolution_status"] = resolver(raw_route, gtfs)
            # Speed units are unknown for MWRTA — keep raw, don't fabricate m/s.
            row["raw_speed"] = _to_float(r.get("speed"))
            row["speed_unit"] = "unknown"
            row["ts_utc"] = naive_local_to_utc(
                r.get("vehicle_datetime"), agency_tz, r.get("feed_timestamp")
            )
            if row["ts_utc"] is None:
                row["ts_utc"] = r.get("feed_timestamp")
            out.append(row)
        return out


@Normalizer.register("routematch_vehicles")
class RouteMatchNormalizer(Normalizer):
    """BRTA: masterRouteId -> short name -> route_id; trip_id cleared; mph -> m/s; ISO+offset -> UTC."""

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        out = []
        for r in rows:
            row = self._base(
                feed,
                r.get("vehicle_id"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                bearing=r.get("heading"),
            )
            raw_route = _str_or_none(r.get("route_id"))
            row["raw_route"] = raw_route
            candidate = brta_candidate_short_name(raw_route)
            if raw_route is None:
                row["resolution_status"] = _NO_ROUTE
            elif candidate and gtfs.route_short_names.get(candidate):
                row["route_id"] = gtfs.route_short_names[candidate]
                row["resolution_status"] = _RESOLVED
            elif raw_route in gtfs.route_ids:
                row["route_id"] = raw_route
                row["resolution_status"] = _PASSTHROUGH
            else:
                row["resolution_status"] = _UNRESOLVED
            # trip_id is RouteMatch-internal and never matches GTFS — clear it.
            row["trip_id"] = None
            row["speed_mps"] = mph_to_mps(r.get("speed"))
            row["speed_unit"] = "mps"
            row["ts_utc"] = iso_offset_to_utc(r.get("last_update"))
            if row["ts_utc"] is None:
                row["ts_utc"] = r.get("feed_timestamp")
            out.append(row)
        return out


@Normalizer.register("trillium_vehicles")
class TrilliumNormalizer(Normalizer):
    """MEVA: route_short_name (already canonical) -> route_id; ISO-Z -> UTC; speed raw."""

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        out = []
        for r in rows:
            row = self._base(
                feed,
                r.get("vehicle_id"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                bearing=r.get("heading_degrees"),
            )
            raw_route = _str_or_none(r.get("route_short_name"))
            row["raw_route"] = raw_route
            row["route_id"], row["resolution_status"] = resolve_route_exact(
                raw_route, gtfs
            )
            # nibble does not convert Trillium speed; keep raw.
            row["raw_speed"] = _to_float(r.get("speed"))
            row["speed_unit"] = "unknown"
            row["ts_utc"] = iso_offset_to_utc(r.get("last_updated"))
            if row["ts_utc"] is None:
                row["ts_utc"] = r.get("feed_timestamp")
            out.append(row)
        return out


@Normalizer.register("swiv_vehicles")
class SwivNormalizer(Normalizer):
    """GATRA/LRTA/WRTA: idLigne -> nomCommercial (topo) -> route_id; km/h -> m/s.

    WRTA tries its static idLigne->short-name map first. Without a topo_map
    (the swiv_ligne table not yet landed) Swiv routes stay unresolved.
    """

    needs_topo = True

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        wrta = agency_id.upper() == "WRTA"
        out = []
        for r in rows:
            row = self._base(
                feed,
                r.get("vehicle_id"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                bearing=r.get("heading"),
            )
            ligne = r.get("route_line_id")
            ligne_s = None if _is_missing(ligne) else str(ligne).strip()
            row["raw_route"] = ligne_s
            route_id, status = None, _NO_ROUTE if ligne_s is None else _UNRESOLVED
            if ligne_s is not None:
                # WRTA static fallback maps idLigne -> short name -> route_id.
                if wrta and ligne_s in _WRTA_LIGNE_TO_SHORT_NAME:
                    rid = gtfs.route_short_names.get(_WRTA_LIGNE_TO_SHORT_NAME[ligne_s])
                    if rid:
                        route_id, status = rid, _RESOLVED
                # topo: idLigne -> nomCommercial -> route_id
                if route_id is None and topo_map:
                    nom = topo_map.get(ligne_s)
                    if nom:
                        rid, st = resolve_route_exact(nom, gtfs)
                        route_id, status = rid, st
            row["route_id"], row["resolution_status"] = route_id, status
            row["speed_mps"] = kmh_to_mps(r.get("speed"))
            row["speed_unit"] = "mps"
            # Swiv carries no per-vehicle timestamp; use the poll time.
            row["ts_utc"] = r.get("feed_timestamp")
            out.append(row)
        return out


@Normalizer.register("vta_vehicles")
class VtaNormalizer(Normalizer):
    """Vineyard VTA: headsignText -> route_id; velocity mph -> m/s; naive-local -> UTC."""

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        out = []
        for r in rows:
            row = self._base(
                feed,
                r.get("vehicle_id"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                bearing=r.get("bearing"),
            )
            raw_route = _str_or_none(r.get("headsign_text"))
            row["raw_route"] = raw_route
            row["route_id"], row["resolution_status"] = resolve_route_exact(
                raw_route, gtfs
            )
            row["speed_mps"] = mph_to_mps(r.get("velocity"))
            row["speed_unit"] = "mps"
            row["ts_utc"] = naive_local_to_utc(
                r.get("last_update"), agency_tz, r.get("feed_timestamp")
            )
            if row["ts_utc"] is None:
                row["ts_utc"] = r.get("feed_timestamp")
            out.append(row)
        return out


@Normalizer.register("passio_vehicles")
class PassioNormalizer(Normalizer):
    """FRTA/MART: route display name -> route_id; trip_id cleared; speed raw; coarse clock + poll date."""

    def normalize(self, rows, gtfs, *, feed, agency_id, agency_tz, topo_map=None):
        out = []
        for r in rows:
            row = self._base(
                feed,
                r.get("vehicle_id"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                bearing=r.get("calculated_course"),
            )
            raw_route = _str_or_none(r.get("route_name"))
            row["raw_route"] = raw_route
            row["route_id"], row["resolution_status"] = resolve_route_passio(
                raw_route, gtfs
            )
            row["trip_id"] = None  # Passio tripId never matches GTFS
            # Passio speed units are unknown — keep raw.
            row["raw_speed"] = _to_float(r.get("speed"))
            row["speed_unit"] = "unknown"
            row["ts_utc"] = combine_clock_with_poll_date(
                r.get("created"), r.get("feed_timestamp"), agency_tz
            )
            out.append(row)
        return out
