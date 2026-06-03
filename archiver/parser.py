from abc import ABC, abstractmethod
import json
from typing import Any, ClassVar
from google.protobuf.message import DecodeError
from archiver.decoder import Decoder
from archiver.logger import logger
from archiver.response import (
    DecodeFailureResponse,
    ErrorResponse,
    FeedResponse,
    JsonResponse,
    ProtobufResponse,
    UnknownResponse,
)
from google.transit.gtfs_realtime_pb2 import FeedMessage


class ParseFailure(Exception):
    """Raised when a Parser cannot parse the input bytes."""


class Parser(ABC):
    # Registration happens as an import side effect — every module defining a
    # @Parser.register(...) subclass must be imported before from_name() is called.
    _registry: ClassVar[dict[str, type["Parser"]]] = {}
    response_cls: ClassVar[type[FeedResponse]]

    @classmethod
    def register(cls, name: str):
        def decorator(subclass: type["Parser"]) -> type["Parser"]:
            if name in cls._registry:
                raise ValueError(
                    f"parser name {name!r} already registered to "
                    f"{cls._registry[name].__name__}"
                )
            cls._registry[name] = subclass
            return subclass

        return decorator

    @classmethod
    def from_name(cls, name: str) -> "Parser":
        try:
            return cls._registry[name]()
        except KeyError:
            raise KeyError(
                f"no parser registered for {name!r}; known: {sorted(cls._registry)}"
            ) from None

    @abstractmethod
    def parse(self, body: bytes) -> Any:
        raise NotImplementedError


@Parser.register("protobuf")
class ProtobufParser(Parser):
    response_cls = ProtobufResponse

    def parse(self, body: bytes) -> FeedMessage:
        feed = FeedMessage()
        try:
            feed.ParseFromString(body)
        except DecodeError as e:
            raise ParseFailure(f"protobuf parse failed: {e}") from e
        return feed


@Parser.register("json")
class JsonParser(Parser):
    response_cls = JsonResponse

    def parse(self, body: bytes) -> list | dict:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise ParseFailure(f"json parse failed: {e}") from e
        return payload


def parse_response(http_response, parser: Parser, decoder: Decoder) -> FeedResponse:
    # 304/dedup are NOT detected here: both need the prior PollState, which lives in
    # archive_one. parse_response stays pure (bytes -> typed response).
    if http_response.status_code >= 400:
        return ErrorResponse(http_response)
    try:
        parsed = parser.parse(http_response.content)
    except ParseFailure:
        return UnknownResponse(http_response)
    drift = decoder.validate(parsed)
    if drift is not None and drift.has_missing_required:
        return DecodeFailureResponse(http_response, drift)
    if drift is not None and drift.extras:
        logger.warning(
            "schema drift (extras only) for %s: %s",
            type(decoder).__name__,
            sorted(drift.extras),
        )
    return parser.response_cls(http_response, parsed=parsed)
