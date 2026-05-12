import pytest

from archiver.parser import (
    JsonParser,
    ParseFailure,
    Parser,
    ProtobufParser,
)
from archiver.response import JsonResponse, ProtobufResponse


def test_protobuf_parser_returns_feed_message(valid_protobuf_bytes):
    parsed = ProtobufParser().parse(valid_protobuf_bytes)
    assert parsed.header.gtfs_realtime_version == "2.0"


def test_protobuf_parser_raises_on_malformed_bytes():
    with pytest.raises(ParseFailure):
        ProtobufParser().parse(b"\x00\x01not protobuf")


def test_json_parser_returns_parsed_payload():
    parsed = JsonParser().parse(b'{"key": "value"}')
    assert parsed == {"key": "value"}


def test_json_parser_raises_on_malformed_bytes():
    with pytest.raises(ParseFailure):
        JsonParser().parse(b"<html>not json</html>")


def test_from_name_resolves_registered_parser():
    assert isinstance(Parser.from_name("protobuf"), ProtobufParser)
    assert isinstance(Parser.from_name("json"), JsonParser)


def test_from_name_raises_for_unknown():
    with pytest.raises(KeyError):
        Parser.from_name("nonexistent")


def test_response_cls_pairing():
    assert ProtobufParser.response_cls is ProtobufResponse
    assert JsonParser.response_cls is JsonResponse
