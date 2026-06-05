from archiver.rate_limit import NullRateLimiter, RateLimiter, TokenBucket


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_starts_full_allows_burst():
    clk = FakeClock()
    b = TokenBucket(capacity=3, refill_rate=1.0, clock=clk)
    # Start-full policy: capacity tokens available immediately, then denied.
    assert [b.try_acquire() for _ in range(4)] == [True, True, True, False]


def test_refills_at_rate():
    clk = FakeClock()
    b = TokenBucket(capacity=2, refill_rate=1.0, clock=clk)  # 1 token/sec
    assert b.try_acquire() and b.try_acquire()
    assert not b.try_acquire()  # drained
    clk.advance(1.0)
    assert b.try_acquire()  # exactly one minted
    assert not b.try_acquire()


def test_refill_caps_at_capacity():
    clk = FakeClock()
    b = TokenBucket(capacity=2, refill_rate=1.0, clock=clk)
    b.try_acquire()
    b.try_acquire()
    clk.advance(100)  # would mint 100, but capacity caps it at 2
    assert [b.try_acquire() for _ in range(3)] == [True, True, False]


def test_fractional_token_denied():
    clk = FakeClock()
    b = TokenBucket(capacity=1, refill_rate=1.0, clock=clk)
    assert b.try_acquire()
    clk.advance(0.5)
    assert not b.try_acquire()  # only 0.5 tokens < 1
    clk.advance(0.5)
    assert b.try_acquire()  # now a full token


def test_null_always_grants():
    n = NullRateLimiter()
    assert all(n.try_acquire() for _ in range(100))


def test_protocol_satisfied_structurally():
    # Neither class inherits RateLimiter; both satisfy it via try_acquire.
    assert isinstance(TokenBucket(1, 1), RateLimiter)
    assert isinstance(NullRateLimiter(), RateLimiter)
