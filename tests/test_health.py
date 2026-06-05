from archiver.health import FeedHealth, is_transient_failure
from archiver.response import TransportErrorResponse


class _FakeResp:
    """Minimal stand-in carrying just the status_code that classification reads."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def test_is_transient_failure_classification():
    assert is_transient_failure(TransportErrorResponse("ConnectError", "boom"))
    assert is_transient_failure(_FakeResp(500))
    assert is_transient_failure(_FakeResp(503))
    assert is_transient_failure(_FakeResp(404))  # >= 400 counts (dead feed)
    assert is_transient_failure(_FakeResp(401))
    assert not is_transient_failure(_FakeResp(200))
    assert not is_transient_failure(_FakeResp(304))  # not-modified is a success


def test_healthy_returns_base_interval():
    h = FeedHealth()
    assert h.next_interval("f", 60) == 60
    assert h.consecutive_failures("f") == 0
    assert not h.is_quarantined("f")


def test_backoff_grows_exponentially():
    h = FeedHealth(quarantine_after=99)  # keep quarantine out of the way
    h.record_failure("f")
    assert h.next_interval("f", 60) == 60  # 60 * 2**0
    h.record_failure("f")
    assert h.next_interval("f", 60) == 120  # 60 * 2**1
    h.record_failure("f")
    assert h.next_interval("f", 60) == 240  # 60 * 2**2


def test_backoff_capped():
    h = FeedHealth(backoff_cap=100, quarantine_after=99)
    for _ in range(10):
        h.record_failure("f")
    assert h.next_interval("f", 60) == 100  # 60*2**9 would be huge; capped


def test_backoff_base_override():
    h = FeedHealth(backoff_base=10, quarantine_after=99)
    h.record_failure("f")
    h.record_failure("f")
    # uses backoff_base (10), not the feed's base_interval (60): 10 * 2**1 = 20
    assert h.next_interval("f", 60) == 20


def test_quarantine_after_k_and_alerts_once():
    h = FeedHealth(quarantine_after=3, quarantine_interval=3600)
    assert h.record_failure("f") is False  # 1
    assert h.record_failure("f") is False  # 2
    assert h.record_failure("f") is True  # 3 -> crosses into quarantine (alert)
    assert h.record_failure("f") is False  # 4 -> already quarantined, no re-alert
    assert h.is_quarantined("f")
    assert h.next_interval("f", 60) == 3600


def test_success_resets_streak():
    h = FeedHealth(quarantine_after=3)
    h.record_failure("f")
    h.record_failure("f")
    assert h.consecutive_failures("f") == 2
    h.record_success("f")
    assert h.consecutive_failures("f") == 0
    assert not h.is_quarantined("f")
    assert h.next_interval("f", 60) == 60


def test_failures_are_per_feed():
    h = FeedHealth(quarantine_after=2)
    h.record_failure("a")
    h.record_failure("a")
    assert h.is_quarantined("a")
    assert not h.is_quarantined("b")
    assert h.next_interval("b", 60) == 60
