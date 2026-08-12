# dashboard/api/services/agency_summary.py

from datetime import date

import pyarrow.compute as pc

from api.services import alerts as alerts_service
from api.services import data
from api.services.agencies import Agency


def get_agency_metrics_summary(
    agency: Agency, start_date: date, end_date: date
) -> dict:
    """One agency's headline metrics over a date range, for the cross-agency
    compare page. Each source is read independently and best-effort — an
    agency with no rail routes simply gets avg_speed_mph=None rather than an
    error, same as the rest of the dashboard's rail-optional handling.
    """
    otp_table = data.read_kind("route_day_otp", agency.feed_names, start_date, end_date)
    matched = (
        int(pc.sum(otp_table.column("matched_count")).as_py() or 0)
        if otp_table.num_rows
        else 0
    )
    on_time = (
        int(pc.sum(otp_table.column("on_time_count")).as_py() or 0)
        if otp_table.num_rows
        else 0
    )

    speed_table = data.read_kind("segment_day", agency.feed_names, start_date, end_date)
    avg_speed_mph = None
    if speed_table.num_rows:
        weighted_sum = 0.0
        total_weight = 0.0
        for speed, samples in zip(
            speed_table.column("speed_p50_mph").to_pylist(),
            speed_table.column("sample_count").to_pylist(),
        ):
            if speed is None:
                continue
            weighted_sum += speed * samples
            total_weight += samples
        avg_speed_mph = weighted_sum / total_weight if total_weight > 0 else None

    delay_summaries = alerts_service.get_line_delays_range(
        agency.feed_names, start_date, end_date
    )

    return {
        "agency_id": agency.agency_id,
        "name": agency.name,
        "on_time_pct": (on_time / matched * 100) if matched > 0 else None,
        "matched_count": matched,
        "avg_speed_mph": avg_speed_mph,
        "total_delay_minutes": sum(s["total_delay_minutes"] for s in delay_summaries),
        "alert_count": sum(s["alert_count"] for s in delay_summaries),
        "delay_alert_count": sum(s["delay_alert_count"] for s in delay_summaries),
    }
