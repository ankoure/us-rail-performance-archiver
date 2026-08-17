"""feed_name -> (agency_id, mdb_feed_id) grouping, shared by gold.py, gtfs.py, and
agency_batch.py. Hoisted out of gold.py/gtfs.py, where it was duplicated verbatim.
"""

from __future__ import annotations

from pathlib import Path

from archiver.loader import load_config


def load_feed_agency_map(config_path: str | Path) -> dict[str, tuple[str, str | None]]:
    """feed_name -> (agency_id, mdb_feed_id_or_None) for GTFS-snapshot resolution.

    A feed's parent agency supplies both the cache-path slug (agency_id) and the
    archived-feeds catalog id (mdb_feed_id).
    """
    config = load_config(str(config_path))
    return {
        feed.name: (agency.agency_id, agency.mdb_feed_id)
        for agency in config.agencies
        for feed in agency.feeds
    }
