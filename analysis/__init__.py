"""The analysis layer: curated parquet → rider-facing metrics.

Exports are resolved lazily (PEP 562 `__getattr__`) so that importing a
dependency-light submodule — e.g. `from analysis.metrics import compute_marts`,
the schedule-free gold path that runs in the prod batch — does not eagerly drag
in the pandas-backed GTFS modules (`static_gtfs`, `gtfs_fetcher`, `event_export`,
`marta_day`). `from analysis import GtfsResolver` still works; it just imports
pandas at first touch rather than at package import.
"""

from __future__ import annotations

import importlib

# name -> submodule it lives in. Lazily imported on first attribute access.
_EXPORTS = {
    # dependency-light (no pandas) — the schedule-free gold path:
    "Stop": "analysis.vehicle_day",
    "Vehicle": "analysis.vehicle_day",
    "VehicleDay": "analysis.vehicle_day",
    "Visit": "analysis.vehicle_day",
    "TripUpdatesDay": "analysis.trip_updates_day",
    "compute_marts": "analysis.metrics",
    "compute_events": "analysis.metrics",
    "STOP_DAY_SCHEMA": "analysis.metrics",
    "ROUTE_DAY_SCHEMA": "analysis.metrics",
    "EVENTS_SCHEMA": "analysis.metrics",
    # gold OTP (adherence): compute_adherence is pandas-free; schedule_index
    # consumes a StaticGtfs frame but imports nothing pandas at module load.
    "compute_adherence": "analysis.adherence",
    "schedule_index": "analysis.adherence",
    "ADHERENCE_SCHEMA": "analysis.adherence",
    "STOP_DAY_OTP_SCHEMA": "analysis.adherence",
    "ROUTE_DAY_OTP_SCHEMA": "analysis.adherence",
    # pandas-backed — only loaded when actually touched:
    "StaticGtfs": "analysis.static_gtfs",
    "GtfsResolver": "analysis.gtfs_fetcher",
    "export_events_csv": "analysis.event_export",
    "MartaArrival": "analysis.marta_day",
    "MartaDay": "analysis.marta_day",
    "MartaTrip": "analysis.marta_day",
    "export_marta_events_csv": "analysis.marta_day",
    "build_alert_snapshot": "analysis.alert_snapshot",
    "write_alert_snapshot": "analysis.alert_snapshot",
    "load_alert_snapshot": "analysis.alert_snapshot",
    "classify_alert": "analysis.alert_classifier",
    "summarize_snapshot": "analysis.alert_classifier",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
