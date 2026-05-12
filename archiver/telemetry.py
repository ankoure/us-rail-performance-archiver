from typing import Protocol, ContextManager, Mapping, runtime_checkable
from contextlib import contextmanager

Tags = Mapping[str, str] | None


class Span(Protocol):
    def set_tag(self, key: str, value: str | int | float | bool) -> None: ...
    def set_error(self, exc: BaseException) -> None: ...


@runtime_checkable
class Telemetry(Protocol):
    def incr(self, metric: str, value: float = 1, tags: Tags = None) -> None: ...
    def gauge(self, metric: str, value: float, tags: Tags = None) -> None: ...
    def histogram(self, metric: str, value: float, tags: Tags = None) -> None: ...
    def timing(self, metric: str, ms: float, tags: Tags = None) -> None: ...
    def span(
        self,
        name: str,
        *,
        resource: str | None = None,
        tags: Tags = None,
    ) -> ContextManager[Span]: ...


class _NoOpSpan:
    def set_tag(self, key: str, value: str | int | float | bool) -> None:
        pass

    def set_error(self, exc: BaseException) -> None:
        pass


class NoOpTelemetry:
    def incr(self, metric: str, value: float = 1, tags: Tags = None) -> None: ...
    def gauge(self, metric: str, value: float, tags: Tags = None) -> None: ...
    def histogram(self, metric: str, value: float, tags: Tags = None) -> None: ...
    def timing(self, metric: str, ms: float, tags: Tags = None) -> None: ...
    @contextmanager
    def span(self, name, *, resource=None, tags=None):
        yield _NoOpSpan()
