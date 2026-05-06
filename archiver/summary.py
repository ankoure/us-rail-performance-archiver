from dataclasses import dataclass
from google.transit.gtfs_realtime_pb2 import FeedMessage


@dataclass
class FeedSummary:
    vehicle_count: int
    trip_update_count: int
    alert_count: int


def summarize_feed(feed: FeedMessage) -> FeedSummary:
    trip_updates, vehicle_positions, service_alerts = 0, 0, 0
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            trip_updates += 1
        if entity.HasField("vehicle"):
            vehicle_positions += 1
        if entity.HasField("alert"):
            service_alerts += 1
    return FeedSummary(
        vehicle_count=vehicle_positions,
        trip_update_count=trip_updates,
        alert_count=service_alerts,
    )
