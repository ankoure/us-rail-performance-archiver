import heapq
import time
from archiver.feed import Feed


class Scheduler:
    def __init__(
        self,
        feeds: list[Feed],
        default_interval: int,
        clock=time.monotonic,
    ):
        self._clock = clock
        self._default_interval = default_interval
        self._seq = 0
        self._heap: list[tuple[float, int, Feed]] = []
        # Seed the heap: every feed starts due "now" so the system polls
        # everything at startup. Loop over feeds and heappush each one.
        now = self._clock()
        for feed in feeds:
            self._push(due_at=now, feed=feed)

    def next_due(self) -> tuple[float, Feed]:
        """Pop and return the (due_at, feed) with the earliest due time."""
        due_at, _seq, feed = heapq.heappop(self._heap)
        return due_at, feed

    def mark_polled(self, feed: Feed) -> None:
        """Reschedule feed for now + its interval (or the default)."""
        interval = feed.poll_interval_seconds or self._default_interval
        due_at = self._clock() + interval
        self._push(due_at=due_at, feed=feed)

    def _push(self, due_at: float, feed: Feed) -> None:
        """Private helper: push with a fresh seq tiebreaker."""
        heapq.heappush(self._heap, (due_at, self._seq, feed))
        self._seq += 1
