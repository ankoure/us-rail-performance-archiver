from archiver.feed import Feed
from archiver.scheduler import Scheduler


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_feed(name: str, interval: int | None = None) -> Feed:
    return Feed(
        name=name,
        path=f"/{name}",
        client=None,  # type: ignore — scheduler never touches it
        parser=None,  # type: ignore
        decoder=None,  # type: ignore
        poll_interval_seconds=interval,
    )


def test_smallest_interval_polls_first():
    # Two feeds with different intervals — after one round, the shorter-interval
    # feed should be due before the longer one.
    clock = FakeClock()
    fast = make_feed("fast", interval=10)
    slow = make_feed("slow", interval=60)
    scheduler = Scheduler([fast, slow], default_interval=30, clock=clock)

    # Drain the initial "due now" entries for both feeds
    _, feed_a = scheduler.next_due()
    scheduler.mark_polled(feed_a)
    _, feed_b = scheduler.next_due()
    scheduler.mark_polled(feed_b)

    due_at, feed = scheduler.next_due()
    assert feed is fast, "shorter interval should surface first"
    assert due_at == 1010.0


def test_mark_polled_reschedules_for_now_plus_interval():
    # Poll a feed at t=1000 with interval=30. Advance the clock. Next time it
    # surfaces from next_due(), due_at should be 1030.
    clock = FakeClock(start=1000.0)
    feed = make_feed("alpha", interval=30)
    scheduler = Scheduler([feed], default_interval=60, clock=clock)
    scheduler.next_due()
    scheduler.mark_polled(feed)
    clock.advance(100)
    due_at, returned_feed = scheduler.next_due()
    assert returned_feed is feed
    assert due_at == 1030.0, f"expected 1030.0, got {due_at}"


def test_feed_without_interval_uses_default():
    # Feed with poll_interval_seconds=None should get rescheduled at
    # now + default_interval.
    clock = FakeClock(start=1000.0)
    feed = make_feed("beta", interval=None)
    scheduler = Scheduler([feed], default_interval=45, clock=clock)

    scheduler.next_due()
    scheduler.mark_polled(feed)

    due_at, returned_feed = scheduler.next_due()
    assert returned_feed is feed
    assert due_at == 1045.0, f"expected 1045.0, got {due_at}"


def test_ties_broken_deterministically():
    # Two feeds that happen to be due at identical times — heappop should
    # return them in insertion order (the seq tiebreaker). The test should
    # NOT raise TypeError about Feed comparison.
    clock = FakeClock(start=1000.0)
    first = make_feed("first", interval=10)
    second = make_feed("second", interval=10)
    # Both seeded at t=1000 - identical due_at, insertion order decides
    scheduler = Scheduler([first, second], default_interval=30, clock=clock)

    _, feed_a = scheduler.next_due()
    _, feed_b = scheduler.next_due()

    assert feed_a is first
    assert feed_b is second
