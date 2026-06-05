from archiver.response import TransportErrorResponse


def is_transient_failure(response) -> bool:
    """Classify a poll outcome for backoff purposes.

    A transport error (no HTTP response at all) or any non-success HTTP status
    (>= 400) counts as a failure. 2xx/3xx do not — which correctly excludes 304
    Not-Modified and schema-drift 200s (a ``DecodeFailureResponse`` carries status
    200, so backoff won't fire on schema drift, which it couldn't fix anyway).
    """
    if isinstance(response, TransportErrorResponse):
        return True
    return response.status_code >= 400


class FeedHealth:
    """Per-feed consecutive-failure tracking that drives the next poll interval.

    Healthy feeds poll at their normal interval. A failing feed backs off
    exponentially (``base * 2**(n-1)``, capped). After ``quarantine_after``
    consecutive failures it drops to a long ``quarantine_interval`` and an alert
    is emitted once (the caller fires it on the boundary-crossing return of
    :meth:`record_failure`). A single success resets the feed to healthy.

    All knobs are constructor params (no config plumbing); state is in-memory
    only — a restart re-discovers feed health on the next poll.
    """

    def __init__(
        self,
        backoff_base: float | None = None,
        backoff_cap: float = 600.0,
        quarantine_after: int = 5,
        quarantine_interval: float = 3600.0,
    ) -> None:
        self._backoff_base = backoff_base  # None => use the feed's own interval
        self._backoff_cap = backoff_cap
        self._quarantine_after = quarantine_after
        self._quarantine_interval = quarantine_interval
        self._failures: dict[str, int] = {}

    def record_success(self, feed_name: str) -> None:
        """Clear the feed's failure streak."""
        self._failures.pop(feed_name, None)

    def record_failure(self, feed_name: str) -> bool:
        """Increment the consecutive-failure count.

        Returns ``True`` exactly on the poll that crosses into quarantine, so the
        caller can emit the quarantine alert once (not on every subsequent fail).
        """
        n = self._failures.get(feed_name, 0) + 1
        self._failures[feed_name] = n
        return n == self._quarantine_after

    def consecutive_failures(self, feed_name: str) -> int:
        return self._failures.get(feed_name, 0)

    def is_quarantined(self, feed_name: str) -> bool:
        return self._failures.get(feed_name, 0) >= self._quarantine_after

    def next_interval(self, feed_name: str, base_interval: float) -> float:
        """Interval to use for this feed's next poll, given its failure streak."""
        n = self._failures.get(feed_name, 0)
        if n == 0:
            return base_interval
        if n >= self._quarantine_after:
            return self._quarantine_interval
        base = self._backoff_base if self._backoff_base is not None else base_interval
        return min(self._backoff_cap, base * (2 ** (n - 1)))
