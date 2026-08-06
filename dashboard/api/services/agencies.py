# dashboard/api/services/agencies.py

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from api.config import settings
from api.services import data

# route_day/segment_day's mode bucket for subway/light-rail/commuter-rail —
# see analysis/static_gtfs.py's _categorize_route_type. "Currently supporting
# rail" means at least one of an agency's feeds has produced a routes manifest
# row in one of these buckets recently, not just that it's configured.
_RAIL_MODES = ["cr", "rapid"]

# Matches data.read_current_routes's own window for "current" routes — the
# manifest is rebuilt daily, so this only needs to tolerate a handful of
# missed days, not track exact freshness.
_ROUTES_LOOKBACK_DAYS = 14


class AgencyNotFound(Exception):
    """Raised when a lookup key doesn't match any agency_id in feeds.yaml."""


@dataclass(frozen=True)
class Agency:
    agency_id: str
    name: str
    timezone: str
    feed_names: list[str]


def _parse_feeds_yaml(path: Path) -> dict[str, Agency]:
    """Parse feeds.yaml once; memoized by lru_cache so repeat calls are free."""
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    agencies: dict[str, Agency] = {}
    for entry in raw.get("agencies", []):
        agency = Agency(
            agency_id=entry["agency_id"],
            name=entry["name"],
            timezone=entry["timezone"],
            feed_names=[feed["name"] for feed in entry.get("feeds", [])],
        )
        agencies[agency.agency_id] = agency

    return agencies


@lru_cache
def _load_agencies() -> dict[str, Agency]:
    return _parse_feeds_yaml(settings.feeds_config_path)


def get_agency(agency_id: str) -> Agency:
    """Raise AgencyNotFound if agency_id isn't in feeds.yaml."""
    agencies = _load_agencies()
    try:
        return agencies[agency_id]
    except KeyError:
        raise AgencyNotFound(f"No agency found with id {agency_id!r}") from None


def list_agencies() -> list[Agency]:
    return list(_load_agencies().values())


def list_rail_agencies() -> list[Agency]:
    """Agencies with at least one rail-mode route in their current routes
    manifest — the subset the speed dashboard has real data for, out of the
    full no-auth-MDB catalog most of which is bus-only."""
    all_agencies = list(_load_agencies().values())
    feed_names = [name for a in all_agencies for name in a.feed_names]
    if not feed_names:
        return []

    table = data.read_kind(
        "routes",
        feed_names,
        date.today() - timedelta(days=_ROUTES_LOOKBACK_DAYS),
        date.today(),
        filters={"mode": _RAIL_MODES},
    )
    rail_feeds = set(table.column("feed").to_pylist())
    return [a for a in all_agencies if rail_feeds.intersection(a.feed_names)]
