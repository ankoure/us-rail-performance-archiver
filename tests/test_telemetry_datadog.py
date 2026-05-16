from archiver.telemetry_datadog import DatadogTelemetry
import pytest
from tests.fakes.dogstatsd import FakeDogStatsd, StatsdCall


def test_incr_passes_through():
    client = FakeDogStatsd()
    dd = DatadogTelemetry(client)
    dd.incr("foo")
    assert client.calls == [StatsdCall("increment", "foo", 1, [])]


def test_default_tags_applied_to_emissions():
    client = FakeDogStatsd()
    dd = DatadogTelemetry(client, default_tags={"env": "prod"})
    dd.incr("foo", tags={"region": "us-east"})
    assert len(client.calls) == 1
    assert set(client.calls[0].tags) == {"env:prod", "region:us-east"}


def test_span_emits_timing_on_success():
    client = FakeDogStatsd()
    dd = DatadogTelemetry(client)
    with dd.span("op"):
        pass
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.kind == "timing"
    assert call.metric == "op.duration"
    assert call.value >= 0
    assert "status:ok" in call.tags


def test_span_records_error_and_reraises():
    client = FakeDogStatsd()
    dd = DatadogTelemetry(client)
    with pytest.raises(RuntimeError):
        with dd.span("op"):
            raise RuntimeError("boom")
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.kind == "timing"
    assert set(call.tags) >= {"status:error", "error_type:RuntimeError"}


def test_set_tag_appears_in_emission():
    client = FakeDogStatsd()
    dd = DatadogTelemetry(client)
    with dd.span("op") as s:
        s.set_tag("user_id", 42)
    assert "user_id:42" in client.calls[0].tags
