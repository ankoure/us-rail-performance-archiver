# dashboard/api/services/route_metadata.py

from functools import lru_cache

import yaml

from api.config import settings

# The gold routes mart only ever persists 4 coarse buckets (see
# analysis/static_gtfs.py::_categorize_route_type); this renames the two
# whose spelling differs from the taxonomy. "bus" is spelled the same in
# both vocabularies, so it needs no entry. Also left out on purpose:
# "rapid", which collapses subway and light rail (GTFS route_type 1 vs 0)
# because the raw route_type int isn't persisted, so there's nothing here to
# split it further automatically -- it falls through resolve_mode()
# unchanged, staying its own honestly-ambiguous bucket until a manual
# override in route_metadata.yaml assigns a specific route to subway_metro
# or light_rail_streetcar.
_DEFAULT_MODE_MAP = {
    "cr": "commuter_rail",
    "other": "ferry_other",
}


@lru_cache
def _load_overrides() -> dict[tuple[str, str], str]:
    """Parse route_metadata.yaml once; tolerant of a missing file so local
    dev environments without any hand-curated overrides still work."""
    path = settings.route_metadata_path
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return {
        (entry["agency_id"], entry["route_id"]): entry["mode"]
        for entry in raw.get("routes", [])
    }


def resolve_mode(agency_id: str, route_id: str, raw_mode: str) -> str:
    """A route's mode for display grouping: a manual override if one exists
    for (agency_id, route_id), else the gold mart's raw_mode normalized onto
    the same taxonomy vocabulary used for agencies (see agencyTypes.ts)."""
    override = _load_overrides().get((agency_id, route_id))
    if override is not None:
        return override
    return _DEFAULT_MODE_MAP.get(raw_mode, raw_mode)
