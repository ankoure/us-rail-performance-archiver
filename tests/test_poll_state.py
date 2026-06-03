from archiver.poll_state import PollState, PollStateStore


def test_state_survives_restart_with_unsafe_name(tmp_path):
    name = "agency/weird-feed"  # the "/" exercises quote/unquote
    state = PollState(
        etag="v1", last_modified="Sun, 02 Jun 2026 12:00:00 GMT", last_digest="abc123"
    )

    store1 = PollStateStore(str(tmp_path / "poll_state"))
    store1.set(name, state)

    store2 = PollStateStore(
        str(tmp_path / "poll_state")
    )  # "restart": fresh store, same dir
    assert store2.get(name) == state
    assert list((tmp_path / "poll_state").glob("*.tmp")) == []


def test_unknown_feed_returns_null_object(tmp_path):
    store1 = PollStateStore(str(tmp_path / "x"))
    assert store1.get("agency/weird-feed") == PollState(
        etag=None, last_modified=None, last_digest=None
    )


def test_in_memory_mode_works():
    name = "agency/weird-feed"  # the "/" exercises quote/unquote
    state = PollState(
        etag="v1", last_modified="Sun, 02 Jun 2026 12:00:00 GMT", last_digest="abc123"
    )

    store = PollStateStore()
    store.set(name, state)
    assert store.get(name) == state
