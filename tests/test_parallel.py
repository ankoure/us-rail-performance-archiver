from datetime import date

import pytest

from archiver import parallel
from tests.fakes.recording_telemetry import RecordingTelemetry


class _Rollup:
    def __init__(self, fail: bool):
        self.fail = fail
        self.telemetry = RecordingTelemetry()

    def rollup_one(self, feed_name, day, *, force=False):
        if self.fail:
            raise RuntimeError("boom")


@pytest.mark.parametrize("fail", [False, True])
def test_run_one_flushes_worker_telemetry(monkeypatch, fail):
    # Pool workers exit without running atexit, so _run_one must flush itself,
    # including when rollup_one raises (that span carries status:error).
    rollup = _Rollup(fail)
    monkeypatch.setattr(parallel, "_WORKER_ROLLUP", rollup)
    day = date(2026, 1, 1)
    if fail:
        with pytest.raises(RuntimeError):
            parallel._run_one("f", day, False)
    else:
        assert parallel._run_one("f", day, False) == ("f", day)
    assert [c.kind for c in rollup.telemetry.calls] == ["flush"]
