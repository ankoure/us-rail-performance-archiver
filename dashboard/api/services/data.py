# dashboard/api/services/data.py

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

import boto3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import yaml

from api.config import settings


@dataclass(frozen=True)
class _S3Config:
    region: str
    hot_bucket: str
    hot_prefix: str


@lru_cache
def _s3_config() -> _S3Config:
    """Just the `s3:` block of feeds.yaml — the API only ever reads the hot
    bucket, so it has no reason to pull in archiver.loader (and with it the
    whole poller/decoder/rollup stack) just to parse three strings."""
    with settings.feeds_config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)["s3"]
    return _S3Config(
        region=raw["region"],
        hot_bucket=raw["hot_bucket"],
        hot_prefix=raw.get("hot_prefix", ""),
    )


# How often to re-resolve S3 credentials and rebuild datasets bound to them.
# AWS_PROFILE sessions (local) and EC2 instance-role creds (prod) both rotate,
# so freezing them forever eventually breaks a long-lived process.
_S3_FS_TTL_SECONDS = 15 * 60

# kind -> (path under the hot bucket, in-file column holding the exact service date)
_KINDS: dict[str, tuple[str, str | None]] = {
    "stop_day": ("metrics/stop_day", "service_date"),
    "route_day": ("metrics/route_day", "service_date"),
    "adherence": ("metrics/adherence", "service_date"),
    "stop_day_otp": ("metrics/stop_day_otp", "service_date"),
    "route_day_otp": ("metrics/route_day_otp", "service_date"),
    "segment_day": ("metrics/segment_day", "service_date"),
    "events": ("metrics/events", "service_date"),
    "gtfs_versions": ("metrics/gtfs_versions", "service_date"),
    "routes": ("metrics/routes", "service_date"),
    "alerts": ("alerts", None),
}

# How far back to look for the routes manifest's most recent day. It's
# rebuilt daily off gold.py's own GTFS resolver (see pipeline/gold.py
# _build_routes) and rarely changes, so a wide-ish window just needs to
# tolerate a handful of missed days, not track exact freshness.
_ROUTES_LOOKBACK_DAYS = 14

# Version-partitioned marts (see docs/design/static-gtfs-normalization.md) live
# at metrics/<kind>/feed=<feed>/version=<version_slug>/data.parquet -- one
# fixed file per (feed, version), not a day-partitioned dataset. gtfs_versions
# (day-partitioned, in _KINDS above) is the pointer from a service_date to the
# version_slug in effect that day. Each value is the columns identifying a row
# in that mart, used to collapse the copy every one of an agency's feeds
# carries of what is really one shared schedule.
_VERSION_KINDS: dict[str, Sequence[str]] = {
    "gtfs_stops": ["stop_id"],
    "gtfs_calendar": ["service_id"],
    "gtfs_calendar_dates": ["service_id", "date"],
    "gtfs_shapes": ["shape_id", "shape_pt_sequence"],
    "gtfs_directions": ["route_id", "direction_id"],
    "route_shapes": ["route_id", "direction_id", "shape_id", "point_sequence"],
    "route_shape_stops": ["route_id", "direction_id", "shape_id", "stop_id"],
}


def _hot_path(subpath: str) -> str:
    s3 = _s3_config()
    return f"{s3.hot_bucket.rstrip('/')}/{s3.hot_prefix}{subpath}"


def _kind_or_raise(kind: str) -> tuple[str, str | None]:
    try:
        return _KINDS[kind]
    except KeyError:
        raise ValueError(
            f"Unknown kind {kind!r}; must be one of {sorted(_KINDS)}"
        ) from None


def _s3_epoch() -> int:
    """Coarse time bucket that advances every _S3_FS_TTL_SECONDS.

    Used as an lru_cache key so the S3FileSystem/datasets below expire and
    rebuild instead of freezing one credential snapshot for the whole process
    lifetime (see _S3_FS_TTL_SECONDS)."""
    return int(time.monotonic() // _S3_FS_TTL_SECONDS)


@lru_cache(maxsize=4)
def _s3_filesystem_for_epoch(_epoch: int) -> pafs.S3FileSystem:
    """Credentials come from boto3's own resolution (AWS_PROFILE locally, EC2
    instance profile in prod) rather than pyarrow's built-in chain, which
    doesn't reliably honor AWS_PROFILE — only explicit env vars, config files'
    default profile, or EC2 instance metadata.
    """
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    return pafs.S3FileSystem(
        region=_s3_config().region,
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        session_token=creds.token,
    )


def _s3_filesystem() -> pafs.S3FileSystem:
    """One S3FileSystem per _S3_FS_TTL_SECONDS window, region pulled from
    feeds.yaml. maxsize=4 on the underlying cache bounds memory to a couple of
    recent epochs rather than accumulating one entry forever."""
    return _s3_filesystem_for_epoch(_s3_epoch())


@lru_cache(maxsize=32)
def _dataset_for_epoch(kind: str, _epoch: int) -> ds.Dataset:
    """Build a hive-partitioned dataset over hot_bucket/hot_prefix/<kind path>."""
    subpath, _ = _kind_or_raise(kind)

    # hot_prefix is plain string concatenation (archiver/shipper.py's _hot_key
    # convention) — a non-empty prefix already carries its own trailing slash,
    # so don't insert one here.
    root = _hot_path(subpath=subpath)
    return ds.dataset(
        root,
        filesystem=_s3_filesystem_for_epoch(_epoch),
        format="parquet",
        partitioning="hive",
    )


def _dataset_for_kind(kind: str) -> ds.Dataset:
    """Dataset for one kind, rebuilt in lockstep with _s3_filesystem() so it
    never ends up holding a Dataset bound to a stale/expired filesystem."""
    return _dataset_for_epoch(kind, _s3_epoch())


def _year_months(start_date: date, end_date: date) -> list[tuple[int, int]]:
    """Every (year, month) the [start_date, end_date] range touches, inclusive."""
    months: list[tuple[int, int]] = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def read_kind(
    kind: str,
    feed_names: list[str],
    start_date: date,
    end_date: date,
    filters: dict[str, list[str]] | None = None,
    limit: int | None = None,
) -> pa.Table:
    """Read one curated kind, pruned to feed_names + [start_date, end_date],
    plus any exact-match column filters (e.g. {"stop_id": [...]})."""
    _, date_col = _kind_or_raise(kind)
    dataset = _dataset_for_kind(kind)

    if filters:
        unknown = set(filters) - set(dataset.schema.names)
        if unknown:
            raise ValueError(
                f"Unknown filter column(s) {sorted(unknown)} for kind {kind!r}; "
                f"valid columns: {sorted(dataset.schema.names)}"
            )
    predicate = pc.field("feed").isin(feed_names)
    if date_col is not None:
        year_months = _year_months(start_date, end_date)
        ym_field = pc.field("year") * 12 + pc.field("month")
        ym_values = [y * 12 + m for y, m in year_months]

        predicate = (
            predicate
            & ym_field.isin(ym_values)
            & (pc.field(date_col) >= start_date.isoformat())
            & (pc.field(date_col) <= end_date.isoformat())
        )
    else:
        ymd_field = pc.field("year") * 10000 + pc.field("month") * 100 + pc.field("day")
        predicate = (
            predicate
            & (
                ymd_field
                >= start_date.year * 10000 + start_date.month * 100 + start_date.day
            )
            & (ymd_field <= end_date.year * 10000 + end_date.month * 100 + end_date.day)
        )

    for column, values in (filters or {}).items():
        predicate = predicate & pc.field(column).isin(values)
    if limit is not None:
        return dataset.head(limit, filter=predicate)
    return dataset.to_table(filter=predicate)


def _dedupe_rows(table: pa.Table, keys: Sequence[str]) -> pa.Table:
    """First row wins for each distinct tuple of `keys`. All of an agency's
    feeds share one underlying schedule, so the same reference row (a route, a
    stop) usually appears under several of them; this collapses those without
    assuming which feed a row came from."""
    columns = [table.column(key).to_pylist() for key in keys]
    seen: set[tuple] = set()
    keep: list[int] = []
    for i, key in enumerate(zip(*columns)):
        if key in seen:
            continue
        seen.add(key)
        keep.append(i)
    return table.take(keep)


def read_current_routes(feed_names: list[str]) -> pa.Table:
    """The routes manifest's most recent day for one agency (route_id,
    route_short_name, route_long_name, mode). Unlike the gtfs_* marts this is
    plain day-partitioned data, not a version pointer -- rebuilt daily, so
    "most recent day within the lookback window" is all "current" means here.

    Reads every one of the agency's feeds rather than picking one: gold.py only
    writes a routes manifest for feeds it could build vehicle-day data from, so
    an agency whose first feed is service alerts has no manifest under that
    feed while its vehicle feed has one. "Most
    recent day" is resolved per feed for the same reason -- one agency-wide
    latest day would drop a feed whose batch is a day behind.
    """
    table = read_kind(
        "routes",
        feed_names,
        date.today() - timedelta(days=_ROUTES_LOOKBACK_DAYS),
        date.today(),
    )
    if table.num_rows == 0:
        return table
    feeds = table.column("feed").to_pylist()
    days = table.column("service_date").to_pylist()
    latest_day: dict[str, str] = {}
    for feed, day in zip(feeds, days):
        if day > latest_day.get(feed, ""):
            latest_day[feed] = day
    current = table.take(
        [i for i, (feed, day) in enumerate(zip(feeds, days)) if latest_day[feed] == day]
    )
    return _dedupe_rows(current, ["route_id"])


def _latest_version_slugs(feed_names: list[str]) -> dict[str, str]:
    """Most recent version_slug per feed, from the gtfs_versions pointer mart.

    Wide-open date range: gtfs_versions is tiny (one row per feed per day), and
    this only ever needs "whatever the newest pointer is," not a specific day.
    Feeds with no pointer at all are absent from the result.
    """
    table = read_kind("gtfs_versions", feed_names, date(2020, 1, 1), date.today())
    latest: dict[str, tuple[str, str]] = {}
    for feed, day, version in zip(
        table.column("feed").to_pylist(),
        table.column("service_date").to_pylist(),
        table.column("version_slug").to_pylist(),
    ):
        if feed not in latest or day > latest[feed][0]:
            latest[feed] = (day, version)
    return {feed: version for feed, (_, version) in latest.items()}


def read_latest_version_mart(kind: str, feed_names: list[str]) -> pa.Table:
    """Read a version-partitioned mart's current data.parquet across an
    agency's feeds and stack the results (see
    docs/design/static-gtfs-normalization.md), each feed resolved via
    gtfs_versions' latest pointer rather than a hive-partitioned day scan.
    Rows are deduped on the mart's identity columns (_VERSION_KINDS), so a row
    carried by several of the agency's feeds is returned once; feeds are read
    in feeds.yaml order, so which copy survives is stable.

    A feed with nothing to contribute is skipped rather than erroring: it may
    have no version pointer yet, or the mart file itself may be absent (e.g.
    the daily batch hasn't run since this mart was added, or that feed's
    schedule doesn't populate the underlying GTFS file). Both are "not
    populated yet," not a client error -- when that holds for every feed the
    result is an empty table.
    """
    if kind not in _VERSION_KINDS:
        raise ValueError(
            f"Unknown version-partitioned kind {kind!r}; must be one of {sorted(_VERSION_KINDS)}"
        )
    version_slugs = _latest_version_slugs(feed_names)
    filesystem = _s3_filesystem()
    tables: list[pa.Table] = []
    for feed_name in feed_names:
        version_slug = version_slugs.get(feed_name)
        if version_slug is None:
            continue
        path = _hot_path(
            f"metrics/{kind}/feed={feed_name}/version={version_slug}/data.parquet"
        )
        try:
            table = pq.read_table(path, filesystem=filesystem)
        except OSError:
            continue
        if table.num_rows:
            tables.append(table)
    if not tables:
        return pa.table({})
    combined = (
        tables[0]
        if len(tables) == 1
        # promote_options: a feed whose mart predates a schema addition still
        # stacks with one written after it, rather than 500ing the endpoint.
        else pa.concat_tables(tables, promote_options="permissive")
    )
    return _dedupe_rows(combined, _VERSION_KINDS[kind])
