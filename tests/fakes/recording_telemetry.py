from dataclasses import dataclass


@dataclass
class Call:
    kind: str  # "incr" | "gauge" | "histogram" | "timing" | "span_enter" | "span_exit" | "span_error"
    name: str
    value: float | None = None
    tags: dict[str, str] | None = None
    resource: str | None = None
