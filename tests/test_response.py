import json

import pytest
from archiver.decoder import MartaJsonDecoder, StandardDecoder
from archiver.parser import JsonParser, ProtobufParser, parse_response
from archiver.response import (
    DecodeFailureResponse,
    ErrorResponse,
    JsonResponse,
    ProtobufResponse,
    UnknownResponse,
)


_MARTA_RECORD = {
    "EVENT_TIME": "05/08/2026 4:35:20 PM",
    "DESTINATION": "Airport",
    "DIRECTION": "S",
    "IS_REALTIME": "true",
    "LINE": "RED",
    "NEXT_ARR": "04:36:34 PM",
    "STATION": "DUNWOODY STATION",
    "TRAIN_ID": "411",
    "WAITING_SECONDS": "64",
    "WAITING_TIME": "1 min",
}


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_error_status_returns_error_response(status, make_response):
    fake = make_response(status_code=status)
    assert isinstance(
        parse_response(fake, ProtobufParser(), StandardDecoder()), ErrorResponse
    )


def test_valid_protobuf_returns_protobuf_response(make_response, valid_protobuf_bytes):
    fake = make_response(status_code=200, content=valid_protobuf_bytes)
    assert isinstance(
        parse_response(fake, ProtobufParser(), StandardDecoder()), ProtobufResponse
    )


def test_invalid_protobuf_returns_unknown_response(make_response):
    fake = make_response(content=b"<html>not actually protobuf</html>")
    assert isinstance(
        parse_response(fake, ProtobufParser(), StandardDecoder()), UnknownResponse
    )


def test_protobuf_response_exposes_parsed_message(make_response, valid_protobuf_bytes):
    fake = make_response(content=valid_protobuf_bytes)
    response = parse_response(fake, ProtobufParser(), StandardDecoder())
    assert isinstance(response, ProtobufResponse)
    assert response.parsed_message().header.gtfs_realtime_version == "2.0"


def test_valid_json_returns_json_response(make_response):
    fake = make_response(status_code=200, content=json.dumps([_MARTA_RECORD]).encode())
    assert isinstance(
        parse_response(fake, JsonParser(), MartaJsonDecoder()), JsonResponse
    )


def test_invalid_json_returns_unknown_response(make_response):
    fake = make_response(content=b"<html>not json</html>")
    assert isinstance(
        parse_response(fake, JsonParser(), MartaJsonDecoder()), UnknownResponse
    )


def test_marta_drift_returns_decode_failure_response(make_response):
    record = {k: v for k, v in _MARTA_RECORD.items() if k != "EVENT_TIME"}
    record["EVT_TIME"] = "05/08/2026 4:35:20 PM"
    fake = make_response(status_code=200, content=json.dumps([record]).encode())
    response = parse_response(fake, JsonParser(), MartaJsonDecoder())
    assert isinstance(response, DecodeFailureResponse)
    meta = response.to_metadata_row()
    assert meta["drift_missing_required"] == ["EVENT_TIME"]
    assert meta["drift_extras"] == ["EVT_TIME"]


def test_marta_extras_only_still_json_response(make_response):
    record = dict(_MARTA_RECORD)
    record["NEW_FIELD"] = "surprise"
    fake = make_response(status_code=200, content=json.dumps([record]).encode())
    assert isinstance(
        parse_response(fake, JsonParser(), MartaJsonDecoder()), JsonResponse
    )
