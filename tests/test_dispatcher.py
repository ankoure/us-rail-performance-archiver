from archiver.dispatcher import Dispatcher
from archiver.scheduler import Scheduler
from tests.test_scheduler import FakeClock, make_feed


class FakeArchiver:
    def __init__(self):
        self.archived = []

    def archive_one(self, feed):
        self.archived.append(feed)


class FailingArchiver:
    def archive_one(self, feed):
        raise RuntimeError("boom")


class SyncExecutor:
    """Runs work immediately instead of in a thread."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class RecordingExecutor:
    """Captures what would have been submitted, but doesn't run it."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))


def test_submit_marks_polled_before_dispatching():
    clock = FakeClock(start=1000.0)
    feed = make_feed("alpha", interval=15)
    scheduler = Scheduler([feed], default_interval=60, clock=clock)
    archiver = FakeArchiver()
    executor = RecordingExecutor()
    dispatcher = Dispatcher(scheduler, archiver, executor)
    _, next_due_feed = scheduler.next_due()
    dispatcher.submit(next_due_feed)
    due_at, next_due_feed = scheduler.next_due()
    assert due_at == 1015.0
    assert len(executor.submitted) == 1
    fn, args, _ = executor.submitted[0]
    assert fn == dispatcher._safe_archive
    assert args == (feed,)


def test_safe_archive_swallows_exceptions():
    clock = FakeClock(start=1000.0)
    feed = make_feed("alpha", interval=15)
    scheduler = Scheduler([feed], default_interval=60, clock=clock)
    archiver = FailingArchiver()
    executor = SyncExecutor()
    dispatcher = Dispatcher(scheduler, archiver, executor)
    _, next_due_feed = scheduler.next_due()
    dispatcher.submit(next_due_feed)


def test_safe_archive_invokes_archive_one():
    clock = FakeClock(start=1000.0)
    feed = make_feed("alpha", interval=15)
    scheduler = Scheduler([feed], default_interval=60, clock=clock)
    archiver = FakeArchiver()
    executor = SyncExecutor()
    dispatcher = Dispatcher(scheduler, archiver, executor)
    _, next_due_feed = scheduler.next_due()
    dispatcher.submit(next_due_feed)
    assert archiver.archived == [feed]
