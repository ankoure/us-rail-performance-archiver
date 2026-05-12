from archiver.telemetry import NoOpTelemetry, Telemetry


def test_no_op_telemetry_is_telemetry():
    isinstance(NoOpTelemetry(), Telemetry)


def test_noop_metric_methods_callable():
    t = NoOpTelemetry()
    t.incr("m")
    t.incr("m", tags={"f": "x"})
    t.gauge("m", 1.0)
    t.gauge("m", 1.0, tags={"f": "x"})
    t.histogram("m", 1.0)
    t.histogram("m", 1.0, tags={"f": "x"})
    t.timing("m", 1.0)
    t.timing("m", 1.0, tags={"f": "x"})
