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
        agency_id="agency",  # value doesn't matter for scheduler tests
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


def test_jitter_zero_is_exact_seed_and_reschedule():
    # Default jitter=0 preserves the original deterministic behavior.
    clock = FakeClock(start=1000.0)
    feed = make_feed("a", interval=30)
    sched = Scheduler([feed], default_interval=60, clock=clock)  # jitter defaults 0
    due_at, _ = sched.next_due()
    assert due_at == 1000.0  # seeded exactly at now
    sched.mark_polled(feed)
    assert sched.next_due()[0] == 1030.0  # exactly now + interval


def test_seed_spread_distributes_first_poll():
    # seed_spread (not jitter) controls the startup spread of first-poll times.
    clock = FakeClock(start=1000.0)
    feed = make_feed("a", interval=100)
    # seed offset = interval * seed_spread * rng() = 100 * 0.5 * 0.5 = 25.0
    sched = Scheduler(
        [feed], default_interval=60, clock=clock, seed_spread=0.5, rng=lambda: 0.5
    )
    due_at, _ = sched.next_due()
    assert due_at == 1025.0


def test_seed_spread_zero_seeds_at_now():
    # Default seed_spread=0 keeps "everything due at now", even with jitter on.
    clock = FakeClock(start=1000.0)
    feed = make_feed("a", interval=100)
    sched = Scheduler([feed], default_interval=60, clock=clock, jitter=0.1)
    assert sched.next_due()[0] == 1000.0


def test_jitter_reschedule_is_symmetric():
    feed = make_feed("a", interval=100)
    # rng=1.0 -> offset = interval*jitter*(2*1-1) = +10  -> 1000 + 100 + 10
    s_hi = Scheduler(
        [feed],
        default_interval=60,
        clock=FakeClock(1000.0),
        jitter=0.1,
        rng=lambda: 1.0,
    )
    s_hi.next_due()  # drain seed (at 1000; seed_spread defaults to 0)
    s_hi.mark_polled(feed)
    assert s_hi.next_due()[0] == 1110.0

    # rng=0.0 -> offset = interval*jitter*(2*0-1) = -10  -> 1000 + 100 - 10
    s_lo = Scheduler(
        [feed],
        default_interval=60,
        clock=FakeClock(1000.0),
        jitter=0.1,
        rng=lambda: 0.0,
    )
    s_lo.next_due()  # drain seed (at 1000; seed_spread defaults to 0)
    s_lo.mark_polled(feed)
    assert s_lo.next_due()[0] == 1090.0


def test_interval_override_used_for_backoff():
    clock = FakeClock(start=1000.0)
    feed = make_feed("a", interval=30)
    sched = Scheduler([feed], default_interval=60, clock=clock)  # jitter 0
    sched.next_due()
    sched.mark_polled(feed, interval=300)  # backoff override, ignores feed's 30
    assert sched.next_due()[0] == 1300.0
