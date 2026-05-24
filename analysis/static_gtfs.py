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
from zoneinfo import ZoneInfo

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
    def __init__(self, zip_path: Path | str) -> None:
        self.zip_path = Path(zip_path)
        if not self.zip_path.exists():
            raise FileNotFoundError(self.zip_path)

    def __repr__(self) -> str:
        return f"StaticGtfs({self.zip_path})"

    def _read(self, name: str, **read_csv_kwargs) -> pd.DataFrame:
        with zipfile.ZipFile(self.zip_path) as z:
            with z.open(name) as f:
                return pd.read_csv(f, **read_csv_kwargs)

    @cached_property
    def trips(self) -> pd.DataFrame:
        return self._read(
            "trips.txt",
            usecols=lambda c: (
                c in {"trip_id", "route_id", "service_id", "direction_id"}
            ),
            dtype={"trip_id": str, "route_id": str, "service_id": str},
        )

    @cached_property
    def stop_times(self) -> pd.DataFrame:
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
        local_tz: ZoneInfo,
    ) -> None:
        """Populate `scheduled_headway` and `scheduled_tt` on each event dict in-place.

        `events` must all share the same service_date (caller buckets first).
        Each event dict must have at minimum: route_id, direction_id, stop_id,
        event_time (str ending in '±HH:MM' local time as produced by event_export).
        """
        if not events:
            return
        sched = self.scheduled_stops(service_date)
        if sched.empty:
            return

        midnight = dt.datetime.combine(service_date, dt.time(), tzinfo=local_tz)
        rows = []
        for idx, ev in enumerate(events):
            t = dt.datetime.fromisoformat(ev["event_time"])
            rows.append(
                {
                    "_idx": idx,
                    "route_id": ev["route_id"],
                    "direction_id": ev["direction_id"],
                    "stop_id": ev["stop_id"],
                    "event_seconds": int((t - midnight).total_seconds()),
                }
            )
        # merge_asof requires both sides globally sorted by the `on` key.
        ev_df = pd.DataFrame(rows).sort_values("event_seconds").reset_index(drop=True)

        # Backward asof on event_seconds within (route, dir, stop): the prior scheduled arrival's
        # headway is what this event "should have" experienced.
        sched_for_join = (
            sched[
                _RTE_DIR_STOP + ["arrival_seconds", "scheduled_headway", "scheduled_tt"]
            ]
            .rename(columns={"arrival_seconds": "event_seconds"})
            .sort_values("event_seconds")
            .reset_index(drop=True)
        )
        merged = pd.merge_asof(
            ev_df,
            sched_for_join,
            on="event_seconds",
            by=_RTE_DIR_STOP,
            direction="backward",
        )

        for _, row in merged.iterrows():
            ev = events[row["_idx"]]
            hw = row["scheduled_headway"]
            tt = row["scheduled_tt"]
            ev["scheduled_headway"] = "" if pd.isna(hw) else int(hw)
            ev["scheduled_tt"] = "" if pd.isna(tt) else int(tt)


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
