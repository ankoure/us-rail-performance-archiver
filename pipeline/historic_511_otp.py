"""Parse 511.org's historic stop_observations.txt into OTP marts.

pipeline/historic_511.py lands the raw monthly 511 "RG" (regional) zip in S3.
That zip's stop_observations.txt is a stop-level log of observed vs.
scheduled arrival/departure, already matched to the schedule by 511 itself —
for the 6 Bay Area rail agencies this project polls live, every row we care
about carries a populated scheduled_arrival_time/scheduled_departure_time
(verified by hand against the 2026-07 archive: 100% populated for CT and SF).
So unlike pipeline/gold.py's live path, this needs no GTFS-join step of its
own: it just reshapes each matched row into the same fact-row shape
analysis.adherence.compute_adherence() would have produced from live polling,
reusing that module's schemas/aggregation/classification so the output is
indistinguishable from a live-derived day.

Ships into the SAME feed-partitioned mart paths the live pipeline uses (see
_AGENCY_TO_FEED), via Shipper.ship_one(hot_only=True) -- the exact pattern
pipeline/gold_backfill.py uses -- so dashboard/api's existing OTP endpoints
pick this up for any date range with no API/frontend changes.

Meant to run as the rail-archiver-historic-511-otp ECS task
(terraform/historic_511_otp.tf, manual-only like gold_backfill), one month at
a time against a month pipeline/historic_511.py has already landed; runs the
same locally.

Example:
    uv run python pipeline/historic_511_otp.py --month 2026-07
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import tempfile
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

# Make the repo root importable when run as `python pipeline/historic_511_otp.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from dotenv import load_dotenv

from analysis.adherence import (
    ADHERENCE_SCHEMA,
    DEFAULT_EARLY_THRESHOLD_S,
    DEFAULT_LATE_THRESHOLD_S,
    ROUTE_DAY_OTP_SCHEMA,
    STOP_DAY_OTP_SCHEMA,
    _aggregate,
    _classify,
    _service_midnight_unix,
)
from analysis.static_gtfs import StaticGtfs, _hms_to_seconds
from archiver.loader import build_shipper, build_uploader, build_telemetry, load_config  # noqa: E402
from archiver.logger import logger  # noqa: E402
from pipeline.gold import _mart_path, _write_parquet  # noqa: E402

load_dotenv()

_LA_TZ = ZoneInfo("America/Los_Angeles")

# 511 agency_id -> the live -vehicles feed name this project already polls
# under (config/feeds.yaml's BAY_AREA_511 agency block). Marts land under
# these SAME feed names so dashboard/api's existing per-feed queries pick
# up historic days automatically.
_AGENCY_TO_FEED = {
    "SF": "sfmta-vehicles",
    "SC": "vta-vehicles",
    "CT": "caltrain-vehicles",
    "CE": "ace-vehicles",
    "AM": "capcor-vehicles",
    "SA": "smart-vehicles",
}

_COLUMNS = [
    "trip_id",
    "service_date",
    "vehicle_id",
    "stop_sequence",
    "observed_arrival_time",
    "observed_departure_time",
    "route_id",
    "agency_id",
    "direction_id",
    "to_stop_id",
    "scheduled_arrival_time",
    "scheduled_departure_time",
]

_CHUNK_ROWS = 500_000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--month", required=True, help="Historic month already landed (YYYY-MM)."
    )
    p.add_argument(
        "--operator",
        default="rg",
        help="Operator prefix under historic/511/ in the hot bucket (default: rg).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="re-ship a (feed, day) partition even if already shipped",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/feeds.yaml",
        help="Path to the feeds config YAML (default: config/feeds.yaml)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _read_matched_frame(zip_path: Path) -> pd.DataFrame:
    """Stream stop_observations.txt out of the zip, filtered to our 6 agencies.

    Never extracts the multi-GB member to disk: zipfile decompresses it as a
    stream straight into pandas' chunked CSV reader.
    """
    chunks = []
    with zipfile.ZipFile(zip_path) as zf, zf.open("stop_observations.txt") as raw:
        for chunk in pd.read_csv(
            raw, usecols=_COLUMNS, dtype=str, chunksize=_CHUNK_ROWS
        ):
            matched = chunk[chunk["agency_id"].isin(_AGENCY_TO_FEED)]
            if not matched.empty:
                chunks.append(matched)
    if not chunks:
        return pd.DataFrame(columns=_COLUMNS)
    return pd.concat(chunks, ignore_index=True)


def _build_fact_frame(df: pd.DataFrame, route_modes: dict[str, str]) -> pd.DataFrame:
    """Reshape matched stop_observations rows into ADHERENCE_SCHEMA fact rows.

    Vectorized over the whole frame: `_hms_to_seconds` (scalar, reused from
    analysis.static_gtfs) is mapped once per time column; the per-service-day
    midnight anchor (`_service_midnight_unix`, timezone-aware and non-trivial)
    is computed once per DISTINCT day in the frame (at most ~31/month), not
    once per row.
    """
    out = df.copy()
    out["service_day"] = pd.to_datetime(out["service_date"], format="%Y%m%d").dt.date

    anchor_map = {
        d: _service_midnight_unix(d, _LA_TZ) for d in out["service_day"].unique()
    }
    out["anchor_unix"] = out["service_day"].map(anchor_map)

    for col in (
        "observed_arrival_time",
        "observed_departure_time",
        "scheduled_arrival_time",
        "scheduled_departure_time",
    ):
        out[f"{col}_s"] = out[col].map(_hms_to_seconds)  # -1 for missing/invalid

    def _unix(seconds_col: str) -> pd.Series:
        seconds = out[seconds_col]
        return (out["anchor_unix"] + seconds).where(seconds >= 0)

    out["arrival_unix"] = _unix("observed_arrival_time_s")
    out["scheduled_arrival_unix"] = _unix("scheduled_arrival_time_s")
    # A visit's departure is its arrival when no separate departure was
    # observed (mirrors analysis.vehicle_day.Visit for single-ping visits) --
    # ADHERENCE_SCHEMA.departure_unix is non-nullable, same as arrival_unix.
    out["departure_unix"] = _unix("observed_departure_time_s").fillna(
        out["arrival_unix"]
    )
    out["scheduled_departure_unix"] = _unix("scheduled_departure_time_s")

    out["arrival_delay_s"] = out["arrival_unix"] - out["scheduled_arrival_unix"]
    out["departure_delay_s"] = out["departure_unix"] - out["scheduled_departure_unix"]
    basis = out["arrival_delay_s"].where(
        out["arrival_delay_s"].notna(), out["departure_delay_s"]
    )

    # Drop rows with no observed arrival at all, or no schedule anchor to
    # judge against -- mirrors compute_adherence's `if basis is None: continue`.
    out = out[out["arrival_unix"].notna() & basis.notna()].copy()
    basis = basis.loc[out.index]

    out["status"] = basis.map(
        lambda d: _classify(int(d), DEFAULT_EARLY_THRESHOLD_S, DEFAULT_LATE_THRESHOLD_S)
    )
    out["on_time"] = out["status"] == "on_time"
    out["feed"] = out["agency_id"].map(_AGENCY_TO_FEED)
    out["stop_id"] = out["to_stop_id"]
    out["route_mode"] = out["route_id"].map(route_modes)
    out["direction_id"] = pd.to_numeric(out["direction_id"], errors="coerce").astype(
        "Int64"
    )
    out["stop_sequence"] = pd.to_numeric(out["stop_sequence"], errors="coerce").astype(
        "Int64"
    )
    # dtype=str read_csv leaves genuinely-blank fields as NaN, not "" — catch both.
    out["vehicle_id"] = (
        out["vehicle_id"].replace("", None).where(out["vehicle_id"].notna(), None)
    )
    out["service_date"] = out["service_day"].map(lambda d: d.isoformat())

    # Nullable Int64 -> plain Python int/None (via object dtype), so
    # to_dict("records") hands pyarrow real ints and Nones, not pd.NA.
    for col in (
        "direction_id",
        "stop_sequence",
        "arrival_unix",
        "scheduled_arrival_unix",
        "departure_unix",
        "scheduled_departure_unix",
        "arrival_delay_s",
        "departure_delay_s",
    ):
        out[col] = out[col].astype("Int64").astype(object).where(out[col].notna(), None)

    return out[list(ADHERENCE_SCHEMA.names) + ["feed"]]


def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)

    telemetry = build_telemetry(config.telemetry)
    uploader = build_uploader(config.s3, telemetry)
    shipper = build_shipper(config)
    curated_dir = config.writer.curated_dir
    hot_bucket = config.s3.hot_bucket

    key = f"{config.s3.hot_prefix}historic/511/{args.operator}/{args.month}-so.zip"
    if not uploader.exists(hot_bucket, key):
        raise RuntimeError(
            f"s3://{hot_bucket}/{key} not found -- run pipeline/historic_511.py "
            f"--month {args.month} first"
        )

    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(uploader.get_bytes(hot_bucket, key))
        tmp.flush()
        logger.info(
            "[%s] downloaded archive, parsing stop_observations.txt", args.month
        )

        df = _read_matched_frame(tmp_path)
        logger.info("[%s] %d rows matched our 6 agencies", args.month, len(df))
        if df.empty:
            return 0

        route_modes = StaticGtfs(tmp_path).route_modes
        fact = _build_fact_frame(df, route_modes)

    shipped = 0
    for (feed, service_date), group in fact.groupby(["feed", "service_date"]):
        day = dt.date.fromisoformat(service_date)
        fact_rows = group.drop(columns=["feed"]).to_dict("records")
        stop_rows = _aggregate(
            [dict(r, feed=feed) for r in fact_rows],
            ("route_id", "direction_id", "stop_id", "service_date"),
            feed,
        )
        route_rows = _aggregate(
            [dict(r, feed=feed) for r in fact_rows],
            ("route_id", "direction_id", "service_date"),
            feed,
            with_stops=True,
        )

        otp_paths = {
            "adherence": _mart_path(curated_dir, "adherence", feed, day),
            "stop_day_otp": _mart_path(curated_dir, "stop_day_otp", feed, day),
            "route_day_otp": _mart_path(curated_dir, "route_day_otp", feed, day),
        }
        if not args.force and all(p.exists() for p in otp_paths.values()):
            logger.info("[%s %s] already built locally — skipping", feed, day)
        else:
            _write_parquet(fact_rows, ADHERENCE_SCHEMA, otp_paths["adherence"])
            _write_parquet(stop_rows, STOP_DAY_OTP_SCHEMA, otp_paths["stop_day_otp"])
            _write_parquet(route_rows, ROUTE_DAY_OTP_SCHEMA, otp_paths["route_day_otp"])

        shipper.ship_one(feed, day, force=args.force, hot_only=True)
        shipped += 1
        logger.info("[%s %s] %d adherence rows shipped", feed, day, len(fact_rows))

    logger.info("[%s] %d (feed, day) partitions shipped", args.month, shipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args()))
