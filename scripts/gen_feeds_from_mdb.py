"""Generate candidate config/feeds.yaml entries from the Mobility Database catalog.

Reads the public MDB catalog CSV (no auth needed), selects US GTFS-rt feeds that
require no API key, groups them by their static feed (one ``static_reference`` =>
one agency), dedupes against the agencies already in config/feeds.yaml, and writes
a *candidate* YAML for human review. It never edits the live config.

Why a candidate file: id/URL dedup is good but not perfect (an agency added by
hand may carry a static id that differs from MDB's), so a human eyeballs the
output before anything is merged.

Usage:
    uv run python scripts/gen_feeds_from_mdb.py \\
        --catalog-url https://bit.ly/catalogs-csv \\
        --existing config/feeds.yaml \\
        --out config/feeds.candidates.yaml

Key data facts (see also: the memory notes / our exploration):
  * gtfs-rt rows carry NO location -> country/region come from the static feed
    reached via ``static_reference`` -> a row whose ``mdb_source_id`` == that ref.
  * ``urls.authentication_type`` == 0 means no key required.
  * each rt row's own ``mdb_source_id`` is unique -> use it as the feed's mdb id.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path
import re
import sys
from typing import Optional
from urllib.parse import urlparse, urlunparse
import requests
import pandas as pd
import warnings

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.logger import logger
from archiver.config import (  # noqa: E402
    AgencyConfig,
    FeedConfig,
    NoAuthConfig,
)

# entity_type -> the suffix used in the feed name and the agency's feed list.
# Combined endpoints (e.g. "vp|tu") are one feed; decide their suffix in feed_name().
ENTITY_SUFFIX: dict[str, str] = {"vp": "vehicles", "tu": "trips", "sa": "alerts"}


# --------------------------------------------------------------------------- #
# 1. Acquire
# --------------------------------------------------------------------------- #
def fetch_catalog(url: str) -> pd.DataFrame:
    """Download and parse the MDB catalog CSV into a DataFrame.

    Reuse the requests + pandas pattern in analysis/gtfs_fetcher.py:51
    (fetch_catalog). The URL may redirect (bit.ly), so allow redirects.
    """
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[gen] error fetching catalog from {url}: {e}", file=sys.stderr)
        sys.exit(1)
    return pd.read_csv(io.StringIO(r.text))


# --------------------------------------------------------------------------- #
# 2. Select + enrich
# --------------------------------------------------------------------------- #
def select_us_noauth_rt(catalog: pd.DataFrame) -> pd.DataFrame:
    """Return the rt rows we want, enriched with country/region/timezone.

    Filter: data_type == "gtfs-rt", urls.authentication_type (NaN->0) == 0,
    status != "deprecated". Then backfill location by joining each row's
    ``static_reference`` to the static row (data_type == "gtfs") whose
    ``mdb_source_id`` equals it; keep only rows resolving to country == "US".

    Add columns the model needs: ``country``, ``region`` (from the static row's
    subdivision/municipality), ``timezone`` (see resolve_timezone). Rows missing
    a static_reference can't be located -> drop them (with a warning).
    """

    # ------------------------------------------------------------------
    # 1. Split static vs. RT
    # ------------------------------------------------------------------
    static = catalog[catalog["data_type"] == "gtfs"].copy()
    rt = catalog[catalog["data_type"] == "gtfs-rt"].copy()

    # ------------------------------------------------------------------
    # 2. Apply RT filters
    # ------------------------------------------------------------------
    # authentication_type: NaN → 0 (public), keep only 0
    rt["urls.authentication_type"] = rt["urls.authentication_type"].fillna(0)
    rt = rt[rt["urls.authentication_type"] == 0]

    # status: drop deprecated rows (NaN rows are *not* deprecated)
    rt = rt[rt["status"] != "deprecated"]

    # ------------------------------------------------------------------
    # 3. Drop rows with no static_reference (can't locate them)
    # ------------------------------------------------------------------
    missing_ref = rt["static_reference"].isna().sum()
    if missing_ref:
        warnings.warn(
            f"{missing_ref} RT row(s) have no static_reference and will be dropped.",
            stacklevel=2,
        )
        logger.warning("%d RT rows dropped: missing static_reference", missing_ref)
    rt = rt[rt["static_reference"].notna()].copy()

    # static_reference can be:
    #   - a plain numeric id, stored as float ("748.0") or string ("748")
    #   - a pipe-separated list of numeric ids ("2598|1270|1326") → use first
    #   - an opaque external ref ("tld-7878", "ntd-90208") → can't resolve, drop
    def _parse_static_ref(val: object) -> Optional[int]:
        """Return the first resolvable integer id, or None."""
        s = str(val).strip()
        # pipe-separated: take the first token
        first = s.split("|")[0].strip()
        try:
            return int(float(first))
        except (ValueError, OverflowError):
            return None

    rt["static_reference"] = rt["static_reference"].map(_parse_static_ref)

    unparseable = rt["static_reference"].isna().sum()
    if unparseable:
        logger.warning(
            "%d RT rows dropped: static_reference is a non-numeric external "
            "identifier (e.g. 'tld-*', 'ntd-*') that can't be joined to a "
            "local static row.",
            unparseable,
        )
    rt = rt[rt["static_reference"].notna()].copy()
    rt["static_reference"] = rt["static_reference"].astype(int)

    # ------------------------------------------------------------------
    # 4. Build a lookup from the static rows (US only to keep it fast,
    #    but we filter RT to US *after* the join so we can emit a useful
    #    warning for non-US rows)
    # ------------------------------------------------------------------
    static_lookup = (
        static[
            [
                "mdb_source_id",
                "location.country_code",
                "location.subdivision_name",
                "location.municipality",
            ]
        ]
        .drop_duplicates("mdb_source_id")
        .set_index("mdb_source_id")
    )

    # Drop the RT rows' own (empty/irrelevant) location columns before
    # joining so the static columns don't collide with them.
    location_cols = [
        "location.country_code",
        "location.subdivision_name",
        "location.municipality",
    ]
    rt = rt.drop(columns=[c for c in location_cols if c in rt.columns])

    rt = rt.join(static_lookup, on="static_reference", how="left")

    # ------------------------------------------------------------------
    # 5. Warn about RT rows whose static reference didn't resolve
    # ------------------------------------------------------------------
    unresolved = rt["location.country_code"].isna().sum()
    if unresolved:
        logger.warning(
            "%d RT rows dropped: static_reference points to no known static row",
            unresolved,
        )
    rt = rt[rt["location.country_code"].notna()]

    # ------------------------------------------------------------------
    # 6. Keep US only
    # ------------------------------------------------------------------
    non_us = (rt["location.country_code"] != "US").sum()
    if non_us:
        logger.debug("%d non-US RT rows dropped", non_us)
    rt = rt[rt["location.country_code"] == "US"].copy()

    # ------------------------------------------------------------------
    # 7. Enrich with country / region / timezone
    # ------------------------------------------------------------------
    rt["country"] = "US"

    rt["region"] = rt["location.subdivision_name"].where(
        rt["location.subdivision_name"].notna(),
        other=rt["location.municipality"],
    )

    rt["timezone"] = rt.apply(
        lambda row: resolve_timezone(
            row.get("location.subdivision_name"),
            row.get("location.municipality"),
        ),
        axis=1,
    )

    tz_missing = rt["timezone"].isna().sum()
    if tz_missing:
        logger.warning(
            "%d RT rows could not be assigned a timezone and may need "
            "manual review (location.subdivision_name / municipality absent "
            "or unrecognised).",
            tz_missing,
        )

    return rt.reset_index(drop=True)


# US state/territory -> IANA timezone. For states spanning multiple zones this is
# the zone covering the overwhelming majority of the population; minority-zone cities
# are handled by _CITY_TZ below.
_SUBDIVISION_TO_TZ: dict[str, str] = {
    "Alabama": "America/Chicago",
    "Alaska": "America/Anchorage",
    "Arizona": "America/Phoenix",
    "Arkansas": "America/Chicago",
    "California": "America/Los_Angeles",
    "Colorado": "America/Denver",
    "Connecticut": "America/New_York",
    "Delaware": "America/New_York",
    "District of Columbia": "America/New_York",
    "Florida": "America/New_York",
    "Georgia": "America/New_York",
    "Hawaii": "Pacific/Honolulu",
    "Idaho": "America/Boise",
    "Illinois": "America/Chicago",
    "Indiana": "America/Indiana/Indianapolis",
    "Iowa": "America/Chicago",
    "Kansas": "America/Chicago",
    "Kentucky": "America/New_York",
    "Louisiana": "America/Chicago",
    "Maine": "America/New_York",
    "Maryland": "America/New_York",
    "Massachusetts": "America/New_York",
    "Michigan": "America/Detroit",
    "Minnesota": "America/Chicago",
    "Mississippi": "America/Chicago",
    "Missouri": "America/Chicago",
    "Montana": "America/Denver",
    "Nebraska": "America/Chicago",
    "Nevada": "America/Los_Angeles",
    "New Hampshire": "America/New_York",
    "New Jersey": "America/New_York",
    "New Mexico": "America/Denver",
    "New York": "America/New_York",
    "North Carolina": "America/New_York",
    "North Dakota": "America/Chicago",
    "Ohio": "America/New_York",
    "Oklahoma": "America/Chicago",
    "Oregon": "America/Los_Angeles",
    "Pennsylvania": "America/New_York",
    "Puerto Rico": "America/Puerto_Rico",
    "Rhode Island": "America/New_York",
    "South Carolina": "America/New_York",
    "South Dakota": "America/Chicago",
    "Tennessee": "America/Chicago",
    "Texas": "America/Chicago",
    "Utah": "America/Denver",
    "Vermont": "America/New_York",
    "Virginia": "America/New_York",
    "Washington": "America/Los_Angeles",
    "West Virginia": "America/New_York",
    "Wisconsin": "America/Chicago",
    "Wyoming": "America/Denver",
}

# Cities in the MINORITY timezone of a split state — they'd be mis-zoned by the
# state-majority map, so override them by municipality. Extend as the skip log
# surfaces new minority-zone agencies on future catalog refreshes.
_CITY_TZ: dict[str, str] = {
    "El Paso": "America/Denver",  # TX, but Mountain (rest of TX is Central)
    "Knoxville": "America/New_York",  # TN, Eastern (state majority is Central)
    "Chattanooga": "America/New_York",  # TN, Eastern
    "Pensacola": "America/Chicago",  # FL panhandle, Central (state majority Eastern)
}


def resolve_timezone(
    subdivision_name: str | None, municipality: str | None
) -> str | None:
    """Best-effort IANA timezone for a US agency from its location.

    AgencyConfig requires a valid IANA tz (config.py:104). The catalog carries no
    tz, so: (1) a known minority-zone city overrides via _CITY_TZ; (2) otherwise
    the state's majority zone via _SUBDIVISION_TO_TZ; (3) None for an unknown
    state, so the caller flags it for manual review rather than guessing.

    (Authoritative-but-heavier alternative if the heuristic ever feels too brittle:
    read agency.txt from the static feed's zip via analysis.gtfs_fetcher.GtfsResolver.)
    """
    if isinstance(municipality, str) and municipality.strip() in _CITY_TZ:
        return _CITY_TZ[municipality.strip()]

    if isinstance(subdivision_name, str) and subdivision_name.strip():
        return _SUBDIVISION_TO_TZ.get(subdivision_name.strip())

    return None


# --------------------------------------------------------------------------- #
# 3. Existing-config dedup keys
# --------------------------------------------------------------------------- #
def load_existing(path: Path) -> tuple[set[int], set[str], set[str]]:
    """Read config/feeds.yaml and return (static mdb ids, full feed URLs, agency ids).

    - static ids: each agency's ``mdb_feed_id`` ("mdb-53") parsed to int 53.
    - full URLs: every feed's ``base_url`` + ``path`` joined, for the URL backstop.
    - agency ids: every existing ``agency_id``, so generated ids stay unique against
      the live config.
    Used by build_agencies to skip feeds we already poll and avoid id collisions.
    """
    with open(path, "r") as file:
        try:
            data = yaml.safe_load(file)
        except yaml.YAMLError as exception:
            raise ValueError(f"Error parsing YAML file: {exception}") from exception

    mdb_ids: set[int] = set()
    feed_urls: set[str] = set()
    agency_ids: set[str] = set()

    for agency in data.get("agencies", []):
        # Parse "mdb-53" -> 53; skip agencies with no or malformed mdb_feed_id
        raw_id = agency.get("mdb_feed_id", "")
        if isinstance(raw_id, str) and raw_id.startswith("mdb-"):
            with contextlib.suppress(ValueError):
                mdb_ids.add(int(raw_id.removeprefix("mdb-")))

        if agency.get("agency_id"):
            agency_ids.add(agency["agency_id"])

        # Join base_url + path for every feed
        base_url = agency.get("base_url", "").rstrip("/")
        for feed in agency.get("feeds", []):
            path_suffix = feed.get("path", "")
            if base_url and path_suffix:
                feed_urls.add(base_url + "/" + path_suffix.lstrip("/"))

    return mdb_ids, feed_urls, agency_ids


# --------------------------------------------------------------------------- #
# 4. Model: rows -> AgencyConfig objects (validation comes free)
# --------------------------------------------------------------------------- #
def split_url(direct_download: str) -> tuple[str, str]:
    """Split a full feed URL into (base_url origin, path+query).

    base_url is the scheme://host (HttpUrl-friendly); path is everything after,
    since FeedConfig.path is appended to base_url (config.py:74).

    The original scheme is preserved: some feeds are http-only and rewriting them
    to https breaks the fetch (SSL WRONG_VERSION_NUMBER). http/https variants of
    the same host are reconciled in build_agencies, not here.
    """
    parsed = urlparse(direct_download)
    base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    # path includes the path component + query string (no fragment)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return base, path


def feed_name(provider_slug: str, entity_type: str, mdb_id: int) -> str:
    """Human-readable, globally-unique-enough feed name, e.g. 'wmata-bus-vehicles'.

    Map entity_type via ENTITY_SUFFIX for single values; DECIDE a scheme for
    combined endpoints ("vp|tu"). Global uniqueness is ultimately guaranteed by
    pydantic at load time + the mdb_feed_id you attach, so prioritize readability.
    """
    # Normalise the slug: lowercase, collapse non-alnum runs to hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", provider_slug.lower()).strip("-")

    # Map entity_type to a readable suffix.
    # Combined types like "vp|tu" get a joined suffix.
    parts = [et.strip() for et in entity_type.split("|")]
    suffixes = [ENTITY_SUFFIX.get(p, p) for p in parts]
    suffix = "-".join(suffixes)  # e.g. "vehicles-trips"

    return f"{slug}-{suffix}-{mdb_id}"


def agency_id_from(provider: str) -> str:
    """Readable UPPER_SNAKE agency_id, matching the hand-curated style (BART, METRO_STL).

    Prefer a trailing parenthetical acronym ("...Authority (OCTA)" -> OCTA); otherwise
    UPPER_SNAKE the whole name. Uniqueness (vs existing config + among generated) is the
    caller's job — it appends the static id on collision.
    """
    m = re.search(r"\(([A-Za-z0-9][A-Za-z0-9\- ]{1,15})\)\s*$", provider)
    token = m.group(1) if m else provider
    return re.sub(r"[^A-Za-z0-9]+", "_", token).strip("_").upper() or "AGENCY"


def build_agencies(
    rt_rows: pd.DataFrame,
    existing_ids: set[int],
    existing_urls: set[str],
    existing_agency_ids: set[str] = frozenset(),
) -> list[AgencyConfig]:
    """Group enriched rt rows into AgencyConfig objects, skipping dupes.

    For each ``static_reference`` group (= one agency):
      * skip the whole group if its static id is in existing_ids, or if its
        timezone couldn't be resolved (split state -> manual review);
      * gather its feeds, dropping any whose URL is already in existing_urls
        (the backstop) and collecting the origins they imply;
      * require a single origin (scheme-normalized) -> the agency base_url; skip
        the group if its feeds span more than one host;
      * build a FeedConfig per kept row (conservative poll_interval, the row's
        own mdb id as FeedConfig.mdb_feed_id) and one AgencyConfig
        (auth=NoAuthConfig, mdb_feed_id="mdb-<static_reference>"). Constructing
        the pydantic objects IS the validation step; build failures are logged
        and skipped so one bad row can't abort the run.
    """
    agencies: list[AgencyConfig] = []
    # Agency name AND agency_id must be globally unique (config validator + shard key).
    # Providers repeat when one operator publishes multiple static feeds (rail + bus),
    # so disambiguate collisions with the static id rather than letting the merged config
    # reject them. Seed the id set with existing config ids so we don't clash with those.
    used_names: set[str] = set()
    used_ids: set[str] = set(existing_agency_ids)

    for static_ref, group in rt_rows.groupby("static_reference"):
        static_ref = int(static_ref)

        # Skip the whole agency group if already present in config
        if static_ref in existing_ids:
            logger.debug(
                "Skipping static_ref %d: already in existing config", static_ref
            )
            continue

        # All rows in a group share location / timezone (from the same static feed).
        # resolve_timezone returns None for split states, but pandas stores that as
        # NaN (a float) — and bool(NaN) is True, so test with pd.isna, not truthiness.
        first = group.iloc[0]
        timezone = first.get("timezone")
        if timezone is None or pd.isna(timezone):
            logger.warning(
                "Skipping static_ref %d: timezone could not be resolved "
                "(subdivision=%r, municipality=%r)",
                static_ref,
                first.get("location.subdivision_name"),
                first.get("location.municipality"),
            )
            continue

        region = str(first.get("region") or "").strip() or None
        provider = str(first.get("provider") or f"agency-{static_ref}").strip()
        # Phase 1: gather kept feeds, keyed by (host, path) so the same endpoint
        # listed under both http and https collapses to one feed. Prefer the https
        # row when both exist; otherwise keep whatever scheme the catalog gave us
        # (some hosts are http-only). dedup against existing config happens here too.
        kept: dict[tuple[str, str], tuple[int, str, str]] = {}  # (host,path)->(id,ent,scheme)
        for _, row in group.iterrows():
            rt_mdb_id = int(row["mdb_source_id"])
            url_raw = str(row.get("urls.direct_download") or row.get("urls.latest") or "")
            if not url_raw:
                logger.warning("Row mdb-%d has no download URL; skipping", rt_mdb_id)
                continue
            base, path = split_url(url_raw)
            full_url = base.rstrip("/") + "/" + path.lstrip("/")
            if full_url in existing_urls:        # URL backstop
                continue
            scheme = urlparse(base).scheme
            key = (urlparse(base).netloc, path)
            prev = kept.get(key)
            if prev is None or (scheme == "https" and prev[2] != "https"):
                kept[key] = (rt_mdb_id, str(row.get("entity_type") or "vp"), scheme)

        if not kept:
            continue

        # Phase 2: one host per agency (base_url is shared). http/https of the same
        # host already collapsed above, so >1 distinct host means genuinely split
        # origins (e.g. CDTA, Kitsap) we can't model with a single base_url.
        hosts = {host for host, _path in kept}
        if len(hosts) > 1:
            logger.warning("Skipping static_ref %d (%s): feeds span hosts %s",
                            static_ref, provider, sorted(hosts))
            continue
        host = hosts.pop()
        scheme = "https" if any(v[2] == "https" for v in kept.values()) else "http"
        base_url = f"{scheme}://{host}"

        # Phase 3: build feeds against that origin, then the agency
        feeds = []
        for (host, path), (rt_mdb_id, entity_type, _scheme) in kept.items():
            name = feed_name(provider, entity_type, rt_mdb_id)
            try:
                feeds.append(FeedConfig(name=name, path=path,
                                        poll_interval_seconds=30,
                                        mdb_feed_id=f"mdb-{rt_mdb_id}"))
            except Exception as exc:
                logger.warning("FeedConfig mdb-%d failed: %s", rt_mdb_id, exc)
        if not feeds:
            continue


        name = provider
        if name in used_names:
            name = f"{provider} (mdb-{static_ref})"
        used_names.add(name)

        agency_id = agency_id_from(provider)
        if agency_id in used_ids:
            agency_id = f"{agency_id}_{static_ref}"
        used_ids.add(agency_id)

        try:
            agency = AgencyConfig(
                agency_id=agency_id,
                name=name,
                base_url=base_url,
                mdb_feed_id=f"mdb-{static_ref}",
                region=region or "unknown",
                timezone=timezone,
                auth=NoAuthConfig(type="none"),
                feeds=feeds,
            )
            agencies.append(agency)
        except Exception as exc:
            logger.warning(
                "Could not build AgencyConfig for static_ref %d: %s", static_ref, exc
            )
            continue

    logger.info(
        "built %d agencies / %d feeds",
        len(agencies),
        sum(len(a.feeds) for a in agencies),
    )
    return agencies


# --------------------------------------------------------------------------- #
# 5. Emit
# --------------------------------------------------------------------------- #
def dump_candidates(agencies: list[AgencyConfig], out_path: Path) -> None:
    """Serialize agencies under an `agencies:` key to a candidate YAML.

    Use model_dump(mode="json", exclude_none=True) so HttpUrl -> str and unset
    optionals drop out, then yaml.safe_dump. This is review fodder, not the live
    config — write a header comment saying so.
    """
    header = (
        "# AUTO-GENERATED CANDIDATE FILE — DO NOT MERGE WITHOUT REVIEW\n"
        "# Produced by scripts/gen_feeds_from_mdb.py\n"
        "# Each agency below was found in the Mobility Database catalog but is\n"
        "# not yet in config/feeds.yaml. Review URLs, names, and timezones\n"
        "# before promoting entries to the live config.\n\n"
    )

    payload = {
        "agencies": [
            a.model_dump(mode="json", exclude_none=True)
            for a in agencies
        ]
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(header)
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--catalog-url", default="https://bit.ly/catalogs-csv")
    p.add_argument("--existing", type=Path, default=Path("config/feeds.yaml"))
    p.add_argument("--out", type=Path, default=Path("config/feeds.candidates.yaml"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog = fetch_catalog(args.catalog_url)
    rt_rows = select_us_noauth_rt(catalog)
    existing_ids, existing_urls, existing_agency_ids = load_existing(args.existing)
    agencies = build_agencies(
        rt_rows, existing_ids, existing_urls, existing_agency_ids
    )
    dump_candidates(agencies, args.out)
    print(
        f"[gen] wrote {len(agencies)} candidate agencies -> {args.out}", file=sys.stderr
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
