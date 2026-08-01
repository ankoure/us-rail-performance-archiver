# dashboard/api/services/agencies.py

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from api.config import settings


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
