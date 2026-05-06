from archiver.summary import summarize_feed
from google.transit.gtfs_realtime_pb2 import FeedMessage


def test_summarize_returns_correct_counts(create_mixed_protobuf):
    data = create_mixed_protobuf(
        vehicle_position_message_count=5,
        trip_update_message_count=2,
        service_alert_message_count=1,
    )
    feed = FeedMessage()
    feed.ParseFromString(data)
    summary = summarize_feed(feed)
    assert summary.vehicle_count == 5
    assert summary.trip_update_count == 2
    assert summary.alert_count == 1
