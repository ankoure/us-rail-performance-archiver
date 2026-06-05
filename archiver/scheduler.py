import heapq
import random
import time
from archiver.feed import Feed


class Scheduler:
    def __init__(
        self,
        feeds: list[Feed],
        default_interval: int,
        clock=time.monotonic,
        jitter: float = 0.0,
        rng=random.random,
    ):
        self._clock = clock
        self._default_interval = default_interval
        self._jitter = jitter  # fraction of interval, e.g. 0.1 => ±10%
        self._rng = rng  # callable -> float in [0.0, 1.0)
        self._seq = 0
        self._heap: list[tuple[float, int, Feed]] = []
        # Seed the heap: every feed starts due ~"now" so the system polls
        # everything at startup. With jitter > 0, a positive-only offset spreads
        # the initial burst across [now, now + jitter*interval), softening the
        # startup thundering herd. With jitter == 0 (the default), every feed is
        # seeded at exactly `now`, preserving the original behavior.
        now = self._clock()
        for feed in feeds:
            interval = feed.poll_interval_seconds or default_interval
            seed_offset = interval * self._jitter * self._rng()
            self._push(due_at=now + seed_offset, feed=feed)

    def next_due(self) -> tuple[float, Feed]:
        """Pop and return the (due_at, feed) with the earliest due time."""
        due_at, _seq, feed = heapq.heappop(self._heap)
        return due_at, feed

    def mark_polled(self, feed: Feed, interval: float | None = None) -> None:
        """Reschedule the feed for now + interval (+ symmetric jitter).

        ``interval`` overrides the feed's own interval — used for backoff and
        quarantine, where the caller passes a FeedHealth-computed value. When
        omitted, falls back to the feed's ``poll_interval_seconds`` or the
        default. The symmetric ±jitter desyncs feeds that share an origin.
        """
        if interval is None:
            interval = feed.poll_interval_seconds or self._default_interval
        due_at = self._clock() + interval + self._jitter_offset(interval)
        self._push(due_at=due_at, feed=feed)

    def _jitter_offset(self, interval: float) -> float:
        # Symmetric: uniform in [-jitter*interval, +jitter*interval).
        return interval * self._jitter * (2 * self._rng() - 1)

    def _push(self, due_at: float, feed: Feed) -> None:
        """Private helper: push with a fresh seq tiebreaker."""
        heapq.heappush(self._heap, (due_at, self._seq, feed))
        self._seq += 1
