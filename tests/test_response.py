import pytest
from archiver.parser import JsonParser, ProtobufParser, parse_response
from archiver.response import (
    ErrorResponse,
    JsonResponse,
    ProtobufResponse,
    UnknownResponse,
)


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_error_status_returns_error_response(status, make_response):
    fake = make_response(status_code=status)
    assert isinstance(parse_response(fake, ProtobufParser()), ErrorResponse)


def test_valid_protobuf_returns_protobuf_response(make_response, valid_protobuf_bytes):
    fake = make_response(status_code=200, content=valid_protobuf_bytes)
    assert isinstance(parse_response(fake, ProtobufParser()), ProtobufResponse)


def test_invalid_protobuf_returns_unknown_response(make_response):
    fake = make_response(content=b"<html>not actually protobuf</html>")
    assert isinstance(parse_response(fake, ProtobufParser()), UnknownResponse)


def test_protobuf_response_exposes_parsed_message(make_response, valid_protobuf_bytes):
    fake = make_response(content=valid_protobuf_bytes)
    response = parse_response(fake, ProtobufParser())
    assert isinstance(response, ProtobufResponse)
    assert response.parsed_message().header.gtfs_realtime_version == "2.0"


def test_valid_json_returns_json_response(make_response):
    fake = make_response(status_code=200, content=b'{"key": "value"}')
    assert isinstance(parse_response(fake, JsonParser()), JsonResponse)


def test_invalid_json_returns_unknown_response(make_response):
    fake = make_response(content=b"<html>not json</html>")
    assert isinstance(parse_response(fake, JsonParser()), UnknownResponse)
