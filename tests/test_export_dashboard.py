"""Unit tests for scripts/export_dashboard.build_payload.

Exercise the pure folding/joining logic on tiny synthetic mart frames — no
parquet, no GTFS — so the route-direction collapse, count-exact on-time %,
name joins, and worst-stop selection are pinned independently of the I/O paths.
"""

from __future__ import annotations

import pandas as pd

from scripts.export_dashboard import build_payload, _weighted_mean


def _route_day_otp() -> pd.DataFrame:
    # RED has two directions; GREEN one. Counts sum; pct recomputes from sums.
    return pd.DataFrame(
        [
            {
                "route_id": "RED",
                "direction_id": 0,
                "matched_count": 100,
                "on_time_count": 80,
                "arr_delay_p50_s": 30.0,
                "arr_delay_p90_s": 120.0,
            },
            {
                "route_id": "RED",
                "direction_id": 1,
                "matched_count": 100,
                "on_time_count": 60,
                "arr_delay_p50_s": 50.0,
                "arr_delay_p90_s": 200.0,
            },
            {
                "route_id": "GREEN",
                "direction_id": 0,
                "matched_count": 50,
                "on_time_count": 45,
                "arr_delay_p50_s": 10.0,
                "arr_delay_p90_s": 40.0,
            },
        ]
    )


def _route_day() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "route_id": "RED",
                "direction_id": 0,
                "visit_count": 300,
                "headway_p50_s": 360.0,
                "dwell_p50_s": 40.0,
                "dwell_p90_s": 90.0,
            },
            {
                "route_id": "RED",
                "direction_id": 1,
                "visit_count": 100,
                "headway_p50_s": 480.0,
                "dwell_p50_s": 60.0,
                "dwell_p90_s": 110.0,
            },
            {
                "route_id": "GREEN",
                "direction_id": 0,
                "visit_count": 200,
                "headway_p50_s": 600.0,
                "dwell_p50_s": 30.0,
                "dwell_p90_s": 70.0,
            },
        ]
    )


def _stop_day_otp() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "route_id": "RED",
                "stop_id": "S1",
                "matched_count": 60,
                "on_time_pct": 40.0,
            },
            {
                "route_id": "RED",
                "stop_id": "S2",
                "matched_count": 80,
                "on_time_pct": 90.0,
            },
            # Below MIN_STOP_MATCHES (20): noisy, must be excluded even though worst.
            {
                "route_id": "GREEN",
                "stop_id": "S3",
                "matched_count": 5,
                "on_time_pct": 10.0,
            },
        ]
    )


ROUTE_NAMES = {"RED": "Red Line", "GREEN": "Green Line"}
STOP_NAMES = {"S1": "Alpha", "S2": "Beta", "S3": "Gamma"}


def _payload(**over):
    kwargs = dict(
        route_day=_route_day(),
        route_day_otp=_route_day_otp(),
        stop_day_otp=_stop_day_otp(),
        route_names=ROUTE_NAMES,
        stop_names=STOP_NAMES,
        service_date="2026-05-20",
        feed="wmata-vehicles",
        worst_n=15,
    )
    kwargs.update(over)
    return build_payload(**kwargs)


def test_on_time_pct_recomputed_from_summed_counts():
    payload = _payload()
    red = next(r for r in payload["routes"] if r["route_id"] == "RED")
    # (80 + 60) / (100 + 100) = 70.0 — NOT the mean of 80% and 60%.
    assert red["matched_count"] == 200
    assert red["on_time_pct"] == 70.0


def test_routes_sorted_worst_on_time_first():
    payload = _payload()
    # RED 70% should come before GREEN 90%.
    assert [r["route_id"] for r in payload["routes"]] == ["RED", "GREEN"]


def test_names_joined_from_maps():
    payload = _payload()
    assert {r["name"] for r in payload["routes"]} == {"Red Line", "Green Line"}


def test_unknown_route_id_falls_back_to_id():
    payload = _payload(route_names={})
    red = next(r for r in payload["routes"] if r["route_id"] == "RED")
    assert red["name"] == "RED"


def test_headway_dwell_visit_weighted():
    payload = _payload()
    red = next(r for r in payload["routes"] if r["route_id"] == "RED")
    # (360*300 + 480*100) / 400 = 390.0
    assert red["headway_p50_s"] == 390.0
    assert red["visit_count"] == 400


def test_worst_stops_excludes_low_match_and_orders_ascending():
    payload = _payload()
    stops = payload["worst_stops"]
    # S3 (5 matches) excluded; S1 (40%) before S2 (90%).
    assert [s["stop_id"] for s in stops] == ["S1", "S2"]
    assert stops[0]["name"] == "Alpha"
    assert stops[0]["route"] == "Red Line"


def test_worst_n_limit():
    payload = _payload(worst_n=1)
    assert len(payload["worst_stops"]) == 1
    assert payload["worst_stops"][0]["stop_id"] == "S1"


def test_missing_otp_still_emits_routes_from_route_day():
    payload = _payload(route_day_otp=pd.DataFrame())
    ids = {r["route_id"] for r in payload["routes"]}
    assert ids == {"RED", "GREEN"}
    red = next(r for r in payload["routes"] if r["route_id"] == "RED")
    assert red.get("on_time_pct") is None
    assert red["headway_p50_s"] == 390.0


def test_weighted_mean_ignores_nan_and_zero_weight():
    vals = pd.Series([10.0, float("nan"), 30.0])
    wts = pd.Series([0, 5, 5])
    # first row zero weight, second row NaN value -> only third counts.
    assert _weighted_mean(vals, wts) == 30.0


def test_weighted_mean_all_missing_returns_none():
    assert _weighted_mean(pd.Series([float("nan")]), pd.Series([1])) is None


# ---------------------------------------------------------------------------
# Slowest-segments panel
# ---------------------------------------------------------------------------


def _segment_day() -> pd.DataFrame:
    # A→B: slow (5 mph), enough samples. B→C: fast (40 mph), enough samples.
    # A→D: slow (3 mph) but only 5 samples — below MIN_SEGMENT_SAMPLES (10).
    return pd.DataFrame(
        [
            {
                "route_id": "RED",
                "direction_id": 0,
                "from_stop_id": "S1",
                "to_stop_id": "S2",
                "service_date": "2026-05-20",
                "sample_count": 30,
                "speed_p50_mph": 5.0,
                "speed_p90_mph": 9.0,
                "speed_mean_mph": 5.5,
                "transit_p50_s": 60,
                "distance_m": 800.0,
            },
            {
                "route_id": "GREEN",
                "direction_id": 0,
                "from_stop_id": "S2",
                "to_stop_id": "S3",
                "service_date": "2026-05-20",
                "sample_count": 20,
                "speed_p50_mph": 40.0,
                "speed_p90_mph": 55.0,
                "speed_mean_mph": 42.0,
                "transit_p50_s": 30,
                "distance_m": 1600.0,
            },
            {
                "route_id": "RED",
                "direction_id": 1,
                "from_stop_id": "S1",
                "to_stop_id": "S3",
                "service_date": "2026-05-20",
                "sample_count": 5,
                "speed_p50_mph": 3.0,
                "speed_p90_mph": 4.0,
                "speed_mean_mph": 3.2,
                "transit_p50_s": 90,
                "distance_m": 600.0,
            },
        ]
    )


def test_slowest_segments_sorted_slowest_first():
    payload = _payload(segment_day=_segment_day())
    segs = payload["slowest_segments"]
    # S1→S2 (5 mph) before S2→S3 (40 mph); S1→S3 (3 mph, 5 samples) excluded.
    assert len(segs) == 2
    assert segs[0]["from_stop_id"] == "S1"
    assert segs[0]["to_stop_id"] == "S2"
    assert segs[0]["speed_p50_mph"] == 5.0


def test_slowest_segments_excludes_low_sample_count():
    payload = _payload(segment_day=_segment_day())
    stop_pairs = [
        (s["from_stop_id"], s["to_stop_id"]) for s in payload["slowest_segments"]
    ]
    assert ("S1", "S3") not in stop_pairs


def test_slowest_segments_names_joined():
    payload = _payload(segment_day=_segment_day())
    first = payload["slowest_segments"][0]
    assert first["from_name"] == "Alpha"
    assert first["to_name"] == "Beta"
    assert first["route"] == "Red Line"


def test_slowest_n_limit():
    payload = _payload(segment_day=_segment_day(), slowest_n=1)
    assert len(payload["slowest_segments"]) == 1
    assert payload["slowest_segments"][0]["from_stop_id"] == "S1"


def test_missing_segment_day_emits_empty_list():
    payload = _payload()  # no segment_day kwarg
    assert payload["slowest_segments"] == []
