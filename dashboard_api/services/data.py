# dashboard_api/services/data.py

from datetime import date
from functools import lru_cache

import boto3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as pafs

from archiver.loader import load_config

DEFAULT_CONFIG_PATH = "config/feeds.yaml"

# kind -> (path under the hot bucket, in-file column holding the exact service date)
_KINDS: dict[str, tuple[str, str | None]] = {
    "stop_day": ("metrics/stop_day", "service_date"),
    "route_day": ("metrics/route_day", "service_date"),
    "adherence": ("metrics/adherence", "service_date"),
    "stop_day_otp": ("metrics/stop_day_otp", "service_date"),
    "route_day_otp": ("metrics/route_day_otp", "service_date"),
    "alerts": ("alerts", None),
}


def _hot_path(subpath: str) -> str:
    config = load_config(DEFAULT_CONFIG_PATH)
    s3 = config.s3
    return f"{s3.hot_bucket.rstrip('/')}/{s3.hot_prefix}{subpath}"


def _kind_or_raise(kind: str) -> tuple[str, str | None]:
    try:
        return _KINDS[kind]
    except KeyError:
        raise ValueError(
            f"Unknown kind {kind!r}; must be one of {sorted(_KINDS)}"
        ) from None


@lru_cache
def _s3_filesystem() -> pafs.S3FileSystem:
    """One S3FileSystem for the process lifetime, region pulled from feeds.yaml.

    Credentials come from boto3's own resolution (AWS_PROFILE locally, EC2
    instance profile in prod) rather than pyarrow's built-in chain, which
    doesn't reliably honor AWS_PROFILE — only explicit env vars, config files'
    default profile, or EC2 instance metadata.
    """
    config = load_config(DEFAULT_CONFIG_PATH)
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    return pafs.S3FileSystem(
        region=config.s3.region,
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        session_token=creds.token,
    )


@lru_cache
def _dataset_for_kind(kind: str) -> ds.Dataset:
    """Build a hive-partitioned dataset over hot_bucket/hot_prefix/<kind path>."""
    subpath, _ = _kind_or_raise(kind)

    # hot_prefix is plain string concatenation (archiver/shipper.py's _hot_key
    # convention) — a non-empty prefix already carries its own trailing slash,
    # so don't insert one here.
    root = _hot_path(subpath=subpath)
    return ds.dataset(
        root,
        filesystem=_s3_filesystem(),
        format="parquet",
        partitioning="hive",
    )


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
