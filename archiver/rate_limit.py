from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    """Structural interface for a rate limiter.

    Any object with a ``try_acquire() -> bool`` method satisfies this
    protocol; explicit inheritance is not required.
    """

    def try_acquire(self) -> bool: ...


class TokenBucket:
    """Continuous token-bucket rate limiter.

    Structurally satisfies ``RateLimiter`` without inheriting from it.

    Parameters
    ----------
    capacity:
        Maximum number of tokens the bucket can hold.  Also the number of
        tokens present at construction (start-full policy).
    refill_rate:
        Tokens added per second.
    clock:
        Callable returning the current time in seconds.  Defaults to
        ``time.monotonic``; pass a fake for deterministic tests.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        clock=time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._clock = clock
        self._tokens: float = capacity  # start full
        self._last_refill: float = clock()

    def try_acquire(self) -> bool:
        """Attempt to consume one token.

        Refills lazily based on elapsed time, then consumes one token if
        available.

        Returns
        -------
        ``True`` if a token was consumed (caller may proceed).
        ``False`` if the bucket is empty (caller should back off).
        """
        now = self._clock()

        # Lazy continuous refill — mint tokens proportional to elapsed time.
        self._tokens = min(
            self._capacity,
            self._tokens + (now - self._last_refill) * self._refill_rate,
        )
        self._last_refill = now

        if self._tokens >= 1:
            self._tokens -= 1
            return True

        return False


class NullRateLimiter:
    """No-op rate limiter — always grants the token.

    Structurally satisfies ``RateLimiter`` without inheriting from it
    (same pattern as ``NoOpTelemetry``).  Use in contexts where rate
    limiting is intentionally disabled (tests, single-agency dev runs,
    unconstrained internal callers).
    """

    def try_acquire(self) -> bool:
        return True
