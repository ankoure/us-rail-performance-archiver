from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Call:
    kind: str  # "incr" | "gauge" | "histogram" | "timing" | "span_enter" | "span_exit" | "span_error"
    name: str
    value: float | None = None
    tags: dict[str, str] | None = None
    resource: str | None = None


class _RecordingSpan:
    def __init__(self, calls: list[Call], span_name: str):
        self._calls = calls
        self._span_name = span_name

    def set_tag(self, key, value):
        self._calls.append(
            Call("span_tag", name=key, value=value, resource=self._span_name)
        )

    def set_error(self, exc):
        self._calls.append(
            Call("span_error", name=self._span_name, value=type(exc).__name__)
        )


class RecordingTelemetry:
    def __init__(self):
        self.calls: list[Call] = []

    def incr(self, metric, value=1, tags=None):
        self.calls.append(Call("incr", metric, value, dict(tags) if tags else None))

    # gauge/histogram/timing analogous


@contextmanager
def span(self, name, *, resource=None, tags=None):
    self.calls.append(
        Call("span_enter", name, tags=dict(tags) if tags else None, resource=resource)
    )
    rec_span = _RecordingSpan(self.calls, name)
    try:
        yield rec_span
    except BaseException as e:
        self.calls.append(Call("span_error", name, value=type(e).__name__))
        raise
    finally:
        self.calls.append(Call("span_exit", name))
