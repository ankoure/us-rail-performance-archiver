# dashboard/api/services/agencies.py

import dataclasses
from dataclasses import dataclass, field
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
    continent: str | None = None
    region: str | None = None
    #: Mode tags -- every mode this agency operates, so a bus+ferry+commuter
    #: rail operator carries all three and is browsable under all three.
    types: list[str] = field(default_factory=list)
    accent_color: str | None = None
    tagline: str | None = None
    logo: str | None = None


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


def _parse_agency_metadata_yaml(path: Path) -> dict[str, dict]:
    """Parse the dashboard-owned agency_metadata.yaml once; keyed by agency_id.

    This file is separate from feeds.yaml (which archiver/config.py parses
    with a strict extra="forbid" pydantic model) so browsing/display
    metadata can evolve without touching the archiver's config schema.
    Tolerant of a missing file so local dev environments that haven't run
    scripts/gen_agency_metadata.py yet still work — every agency just has
    null continent/region and no mode tags.
    """
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return {entry["agency_id"]: entry for entry in raw.get("agencies", [])}


def _types_of(meta: dict) -> list[str]:
    """Mode tags for one metadata entry.

    Falls back to the pre-tag scalar `type` so an agency_metadata.yaml
    generated before the tag migration still classifies (as a single tag)
    instead of going silently untagged.
    """
    types = meta.get("types")
    if types:
        return list(types)
    scalar = meta.get("type")
    return [scalar] if scalar else []


@lru_cache
def _load_agencies() -> dict[str, Agency]:
    base = _parse_feeds_yaml(settings.feeds_config_path)
    metadata = _parse_agency_metadata_yaml(settings.agency_metadata_path)

    agencies: dict[str, Agency] = {}
    for agency_id, agency in base.items():
        meta = metadata.get(agency_id, {})
        agencies[agency_id] = dataclasses.replace(
            agency,
            continent=meta.get("continent"),
            region=meta.get("region"),
            types=_types_of(meta),
            accent_color=meta.get("accent_color"),
            tagline=meta.get("tagline"),
            logo=meta.get("logo"),
        )

    return agencies


def get_agency(agency_id: str) -> Agency:
    """Raise AgencyNotFound if agency_id isn't in feeds.yaml."""
    agencies = _load_agencies()
    try:
        return agencies[agency_id]
    except KeyError:
        raise AgencyNotFound(f"No agency found with id {agency_id!r}") from None


def list_agencies() -> list[Agency]:
    return list(_load_agencies().values())
