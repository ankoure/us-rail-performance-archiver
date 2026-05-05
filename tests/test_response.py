import pytest
from archiver.response import (
    ErrorResponse,
    ProtobufResponse,
    UnknownResponse,
    parse_response,
)


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_4xx_or_5xx_returns_error_response(status, make_response):
    fake = make_response(status_code=status)
    assert isinstance(parse_response(fake), ErrorResponse)


def test_protobuf_header_with_valid_body_returns_protobuf_response(
    make_response, valid_protobuf_bytes
):
    fake = make_response(
        status_code=200,
        headers={"Content-Type": "protobuf"},
        content=valid_protobuf_bytes,  # no ()
    )
    assert isinstance(parse_response(fake), ProtobufResponse)


@pytest.mark.parametrize(
    "headers", [{"Content-Type": "text/html"}, {}, {"Content-Type": ""}]
)
def test_format_hint_overrides_misleading_header(
    headers, make_response, valid_protobuf_bytes
):
    fake = make_response(status_code=200, headers=headers, content=valid_protobuf_bytes)
    assert isinstance(
        parse_response(fake, expected_format="protobuf"), ProtobufResponse
    )


def test_format_hint_falls_through_when_body_isnt_protobuf(make_response):
    fake = make_response(content=b"<html>not actually protobuf</html>")
    assert isinstance(parse_response(fake, expected_format="protobuf"), UnknownResponse)


def test_protobuf_response_exposes_parsed_message(make_response, valid_protobuf_bytes):
    fake = make_response(
        headers={"Content-Type": "application/x-protobuf"},
        content=valid_protobuf_bytes,
    )
    response = parse_response(fake)
    assert isinstance(response, ProtobufResponse)
    assert response.parsed_message().header.gtfs_realtime_version == "2.0"
