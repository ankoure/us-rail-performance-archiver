from dataclasses import dataclass


@dataclass
class StatsdCall:
    kind: str  # "increment" | "gauge" | "histogram" | "timing"
    metric: str
    value: float
    tags: list[str] | None = None


class FakeDogStatsd:
    """Records calls instead of sending to a real agent.
    Mirrors the DogStatsd interface that DatadogTelemetry uses."""

    def __init__(self) -> None:
        self.calls: list[StatsdCall] = []

    def increment(self, metric, value=1, tags=None):
        self.calls.append(StatsdCall("increment", metric, value, tags))

    def gauge(self, metric, value, tags=None):
        self.calls.append(StatsdCall("gauge", metric, value, tags))

    def histogram(self, metric, value, tags=None):
        self.calls.append(StatsdCall("histogram", metric, value, tags))

    def timing(self, metric, value, tags=None):
        self.calls.append(StatsdCall("timing", metric, value, tags))
