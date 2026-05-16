from contextlib import contextmanager
from time import monotonic
from datadog.dogstatsd.base import DogStatsd


class _DatadogSpan:
    """Accumulates tags + measures duration; emitted by the surrounding
    span() context manager as a timing metric."""

    def __init__(
        self, name: str, resource: str | None, initial_tags: dict[str, str]
    ) -> None:
        self.name = name
        self.tags = dict(initial_tags)
        self.tags["resource"] = resource
        self._start = monotonic()

    def set_tag(self, key: str, value) -> None:
        self.tags[key] = str(value)

    def set_error(self, exc: BaseException) -> None:
        self.tags["error_type"] = type(exc).__name__
        self.tags["status"] = "error"

    def _duration_ms(self) -> float:
        return (monotonic() - self._start) * 1000

    def _final_tags(
        self,
    ) -> dict[str, str]:
        final_dict = dict(self.tags)
        final_dict.setdefault("status", "ok")
        return final_dict


class DatadogTelemetry:
    def __init__(
        self, client: DogStatsd, default_tags: dict[str, str] | None = None
    ) -> None:
        self.client = client
        self.default_tags = default_tags or {}

    def incr(self, metric, value=1, tags=None):
        self.client.increment(metric, value, tags=self._format_tags(tags))

    def gauge(self, metric, value, tags=None):
        self.client.gauge(metric, value, tags=self._format_tags(tags))

    def histogram(self, metric, value, tags=None):
        self.client.histogram(metric, value, tags=self._format_tags(tags))

    def timing(self, metric, ms, tags=None):
        self.client.timing(metric, ms, tags=self._format_tags(tags))

    @contextmanager
    def span(self, name, *, resource=None, tags=None):
        merged = {**self.default_tags, **(tags or {})}
        span = _DatadogSpan(name, resource, merged)
        try:
            yield span
        except Exception as exc:
            span.set_error(exc)
            raise
        finally:
            self.client.timing(
                f"{name}.duration",
                span._duration_ms(),
                tags=self._format_tags(span._final_tags()),
            )

    def _format_tags(self, tags: dict[str, str] | None) -> list[str]:
        merged = {**self.default_tags, **(tags or {})}
        return [f"{k}:{v}" for k, v in merged.items()]
