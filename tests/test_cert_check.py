import pytest

from pipeline.cert_check import _https_host


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://gtfsr.tri-rail.com", "gtfsr.tri-rail.com"),
        ("https://metromap.cityofmadison.com/", "metromap.cityofmadison.com"),
        # Plain-http agencies never handshake TLS when polled, so their :443
        # cert is irrelevant and must not be probed (false expiry alarms).
        ("http://openov.lu/", None),
        ("http://realtime.ripta.com:81/", None),
    ],
)
def test_https_host(base_url, expected):
    assert _https_host(base_url) == expected
