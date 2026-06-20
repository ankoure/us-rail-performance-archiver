"""Export a static JSON bundle for the public-facing performance dashboard.

This is the serving layer the gold marts were missing: it reads one (feed, day)
of the curated metrics marts, joins human-readable route/stop names from the
agency's GTFS static snapshot, and writes a single compact JSON file that a
no-build static page (site/) can `fetch` and chart.

Scope is deliberately small — one feed, one day, a single-day snapshot — to
validate the public-facing direction before investing in a multi-feed pipeline.
WMATA is the pilot because its marts are already in canonical form on disk.

Marts consumed (under curated/metrics/{mart}/feed=.../year=/month=/day=):
  - route_day_otp  -> on-time % + delay percentiles, per (route, direction)
  - route_day      -> headway / dwell percentiles, per (route, direction)
  - stop_day_otp   -> per-stop on-time %, mined for the worst-N stops

The two route-grain marts are folded from (route, direction) to route so the
page shows one bar per line: counts are summed and on_time_pct is recomputed
from the summed counts (exact); percentile columns are matched/visit-weighted
means of the per-direction values (approximate — flagged in the payload).

Examples:

    # the pilot day (the one with OTP marts on disk)
    uv run python scripts/export_dashboard.py \\
        --feed wmata-vehicles --day 2026-05-20

    # custom output location / more stops in the worst list
    uv run python scripts/export_dashboard.py \\
        --feed wmata-vehicles --day 2026-05-20 \\
        --out-dir site/data --worst-n 25
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import pandas as pd

# Make the repo root importable when run as `python scripts/export_dashboard.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gold import _mart_path, load_feed_agency_map  # noqa: E402

DEFAULT_CONFIG = Path("config/feeds.yaml")
DEFAULT_CURATED_DIR = Path("curated")
DEFAULT_OUT_DIR = Path("site/data")
# A per-stop OTP figure on a handful of matched arrivals is noise, not signal;
# require at least this many matches before a stop is eligible for the worst list.
MIN_STOP_MATCHES = 20


def _read_mart(curated_dir: Path, mart: str, feed: str, day: dt.date) -> pd.DataFrame:
    """Load one (feed, day) mart parquet, or an empty frame if absent."""
    path = _mart_path(curated_dir, mart, feed, day)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def _weighted_mean(values: pd.Series, weights: pd.Series) -> float | None:
    """Weighted mean over rows where both value and weight are present/positive.

    Used to collapse per-direction percentile columns (headway/dwell/delay) to a
    single route figure. It is an approximation — a weighted mean of medians is
    not the true median — but it is monotonic and good enough for the PoC; the
    payload marks these fields `approx`.
    """
    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return None
    v = values[mask].astype(float)
    w = weights[mask].astype(float)
    total = w.sum()
    if total <= 0:
        return None
    return float((v * w).sum() / total)


def _clean(value) -> object:
    """JSON-safe scalar: NaN/NaT/inf -> None, numpy scalars -> Python scalars."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if pd.isna(value):
        return None
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def _round(value: float | None, ndigits: int = 1) -> float | None:
    return None if value is None else round(value, ndigits)


def build_payload(
    route_day: pd.DataFrame,
    route_day_otp: pd.DataFrame,
    stop_day_otp: pd.DataFrame,
    route_names: dict[str, str],
    stop_names: dict[str, str],
    service_date: str,
    feed: str,
    worst_n: int,
) -> dict:
    """Fold the marts into the dashboard JSON payload (pure; no I/O).

    Returns a dict with `routes` (one entry per route, joined OTP + headway/dwell)
    and `worst_stops` (the lowest-on-time stops with enough matches to be real).
    Kept free of parquet/GTFS so it can be unit-tested on synthetic frames.
    """
    routes: dict[str, dict] = {}

    # On-time + delay, folded from (route, direction) to route.
    if not route_day_otp.empty:
        for rid, grp in route_day_otp.groupby("route_id"):
            matched = int(grp["matched_count"].fillna(0).sum())
            on_time = int(grp["on_time_count"].fillna(0).sum())
            routes[rid] = {
                "route_id": rid,
                "name": route_names.get(rid, rid),
                "matched_count": matched,
                "on_time_count": on_time,
                "on_time_pct": _round(100 * on_time / matched) if matched else None,
                "arr_delay_p50_s": _round(
                    _weighted_mean(grp["arr_delay_p50_s"], grp["matched_count"])
                ),
                "arr_delay_p90_s": _round(
                    _weighted_mean(grp["arr_delay_p90_s"], grp["matched_count"])
                ),
            }

    # Headway / dwell, folded the same way (weighted by visit_count).
    if not route_day.empty:
        for rid, grp in route_day.groupby("route_id"):
            entry = routes.setdefault(
                rid, {"route_id": rid, "name": route_names.get(rid, rid)}
            )
            entry["visit_count"] = int(grp["visit_count"].fillna(0).sum())
            entry["headway_p50_s"] = _round(
                _weighted_mean(grp["headway_p50_s"], grp["visit_count"])
            )
            entry["dwell_p50_s"] = _round(
                _weighted_mean(grp["dwell_p50_s"], grp["visit_count"])
            )
            entry["dwell_p90_s"] = _round(
                _weighted_mean(grp["dwell_p90_s"], grp["visit_count"])
            )

    # Sort routes by on-time % (worst first surfaces the story), names as tiebreak.
    route_list = sorted(
        routes.values(),
        key=lambda r: (
            r.get("on_time_pct") if r.get("on_time_pct") is not None else 999,
            r.get("name", ""),
        ),
    )

    worst_stops: list[dict] = []
    if not stop_day_otp.empty:
        eligible = stop_day_otp[
            stop_day_otp["matched_count"].fillna(0) >= MIN_STOP_MATCHES
        ]
        worst = eligible.sort_values("on_time_pct", ascending=True).head(worst_n)
        for row in worst.itertuples(index=False):
            rid = _clean(getattr(row, "route_id", None))
            sid = _clean(getattr(row, "stop_id", None))
            worst_stops.append(
                {
                    "stop_id": sid,
                    "name": stop_names.get(sid, sid),
                    "route": route_names.get(rid, rid),
                    "on_time_pct": _round(_clean(getattr(row, "on_time_pct", None))),
                    "matched_count": _clean(getattr(row, "matched_count", None)),
                }
            )

    return {
        "feed": feed,
        "service_date": service_date,
        "generated_note": "Single-day proof-of-concept snapshot.",
        "approx_fields": [
            "arr_delay_p50_s",
            "arr_delay_p90_s",
            "headway_p50_s",
            "dwell_p50_s",
            "dwell_p90_s",
        ],
        "routes": route_list,
        "worst_stops": worst_stops,
    }


def _name_maps(
    feed: str, day: dt.date, config_path: Path, cache_dir: Path, api_url: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    """Build (route_id -> name, stop_id -> name) from the agency GTFS snapshot.

    Returns empty maps (so the export still runs, ids stand in for names) when the
    feed has no mdb_feed_id or the snapshot can't be resolved/fetched.
    """
    from analysis.gtfs_fetcher import GtfsResolver

    feed_agency = load_feed_agency_map(config_path)
    info = feed_agency.get(feed)
    if info is None or not info[1]:
        print(f"[{feed}] no mdb_feed_id — emitting raw ids as names", file=sys.stderr)
        return {}, {}
    agency_id, mdb_id = info
    kwargs = {"cache_dir": cache_dir}
    if api_url:
        kwargs["api_url"] = api_url
    try:
        gtfs = GtfsResolver(mdb_id, agency_id.lower(), **kwargs).for_date(day)
    except (LookupError, FileNotFoundError, OSError) as e:
        print(f"[{feed}] GTFS lookup failed ({e}) — raw ids as names", file=sys.stderr)
        return {}, {}

    routes = gtfs.routes
    route_names: dict[str, str] = {}
    for row in routes.itertuples(index=False):
        rid = getattr(row, "route_id", None)
        if not isinstance(rid, str):
            continue
        # Prefer the long name ("Red") for the public label; fall back to the
        # short badge ("R") and finally the raw id.
        long = getattr(row, "route_long_name", None)
        short = getattr(row, "route_short_name", None)
        name = long if isinstance(long, str) and long.strip() else short
        route_names[rid] = name if isinstance(name, str) and name.strip() else rid

    stops = gtfs.stops
    stop_names: dict[str, str] = {}
    for row in stops.itertuples(index=False):
        sid = getattr(row, "stop_id", None)
        if not isinstance(sid, str):
            continue
        name = getattr(row, "stop_name", None)
        stop_names[sid] = name if isinstance(name, str) and name.strip() else sid

    return route_names, stop_names


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--feed", default="wmata-vehicles", help="curated feed name")
    p.add_argument("--day", required=True, help="service date, YYYY-MM-DD")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--curated-dir", type=Path, default=DEFAULT_CURATED_DIR)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--worst-n", type=int, default=15, help="how many worst stops")
    p.add_argument("--gtfs-cache-dir", type=Path, default=Path("static_gtfs"))
    p.add_argument("--gtfs-api-url", default=None)
    args = p.parse_args(argv)

    day = dt.date.fromisoformat(args.day)

    route_day = _read_mart(args.curated_dir, "route_day", args.feed, day)
    route_day_otp = _read_mart(args.curated_dir, "route_day_otp", args.feed, day)
    stop_day_otp = _read_mart(args.curated_dir, "stop_day_otp", args.feed, day)

    if route_day.empty and route_day_otp.empty:
        print(
            f"[{args.feed} {args.day}] no route_day / route_day_otp marts on disk — "
            "nothing to export",
            file=sys.stderr,
        )
        return 1

    route_names, stop_names = _name_maps(
        args.feed, day, args.config, args.gtfs_cache_dir, args.gtfs_api_url
    )

    payload = build_payload(
        route_day,
        route_day_otp,
        stop_day_otp,
        route_names,
        stop_names,
        service_date=args.day,
        feed=args.feed,
        worst_n=args.worst_n,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.feed}-{args.day}.json"
    out_path.write_text(json.dumps(payload, indent=2))

    named = sum(1 for r in payload["routes"] if r["name"] != r["route_id"])
    print(
        f"[{args.feed} {args.day}] wrote {out_path} — "
        f"{len(payload['routes'])} routes ({named} named), "
        f"{len(payload['worst_stops'])} worst stops"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
