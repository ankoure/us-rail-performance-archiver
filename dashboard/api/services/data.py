# dashboard/api/services/data.py

import time
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
# version_slug in effect that day.
_VERSION_KINDS = frozenset(
    {
        "gtfs_stops",
        "gtfs_calendar",
        "gtfs_calendar_dates",
        "gtfs_shapes",
        "gtfs_directions",
    }
)


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


def read_current_routes(feed_name: str) -> pa.Table:
    """The routes manifest's most recent day for one feed (route_id,
    route_short_name, route_long_name, mode). Unlike the gtfs_* marts this is
    plain day-partitioned data, not a version pointer -- rebuilt daily, so
    "most recent day within the lookback window" is all "current" means here.
    """
    table = read_kind(
        "routes",
        [feed_name],
        date.today() - timedelta(days=_ROUTES_LOOKBACK_DAYS),
        date.today(),
    )
    if table.num_rows == 0:
        return table
    latest_day = max(table.column("service_date").to_pylist())
    return table.filter(pc.field("service_date") == latest_day)


def _latest_version_slug(feed_name: str) -> str | None:
    """Most recent version_slug for a feed, from the gtfs_versions pointer mart.

    Wide-open date range: gtfs_versions is tiny (one row per feed per day), and
    this only ever needs "whatever the newest pointer is," not a specific day.
    """
    table = read_kind("gtfs_versions", [feed_name], date(2020, 1, 1), date.today())
    if table.num_rows == 0:
        return None
    dates = table.column("service_date").to_pylist()
    versions = table.column("version_slug").to_pylist()
    latest = max(range(len(dates)), key=lambda i: dates[i])
    return versions[latest]


def read_latest_version_mart(kind: str, feed_name: str) -> pa.Table:
    """Read a version-partitioned mart's current data.parquet for one feed
    (see docs/design/static-gtfs-normalization.md), resolved via
    gtfs_versions' latest pointer rather than a hive-partitioned day scan.

    Returns an empty table -- not an error -- when there's no version pointer
    yet or the mart file itself is absent (e.g. the daily batch hasn't run
    since this mart was added, or this feed's schedule doesn't populate the
    underlying GTFS file). Both are "not populated yet," not a client error.
    """
    if kind not in _VERSION_KINDS:
        raise ValueError(
            f"Unknown version-partitioned kind {kind!r}; must be one of {sorted(_VERSION_KINDS)}"
        )
    version_slug = _latest_version_slug(feed_name)
    if version_slug is None:
        return pa.table({})
    path = _hot_path(
        f"metrics/{kind}/feed={feed_name}/version={version_slug}/data.parquet"
    )
    try:
        return pq.read_table(path, filesystem=_s3_filesystem())
    except OSError:
        return pa.table({})
