from datetime import datetime, timezone
from abc import ABC
from google.protobuf.message import DecodeError
from archiver.summary import summarize_feed
from google.transit.gtfs_realtime_pb2 import FeedMessage
from dataclasses import asdict


class ArchivableEvent(ABC):
    def __init__(self) -> None:
        self._timestamp = datetime.now(timezone.utc).timestamp()

    def get_timestamp(self) -> float:
        return self._timestamp

    def get_datetime(self) -> datetime:
        return datetime.fromtimestamp(self._timestamp, tz=timezone.utc)

    def to_metadata_row(self) -> dict:
        return {
            "timestamp": self._timestamp,
            **self._extra_metadata(),
        }

    def raw_payload(self) -> None:
        return None

    def _extra_metadata(self) -> dict:
        """Subclasses override to add extra fields. Default none

        Returns:
            dict: _description_
        """
        return {}


class FeedResponse(ArchivableEvent):
    def __init__(self, http_response):
        super().__init__()
        self._http = http_response

    @property
    def status_code(self):
        return self._http.status_code

    @property
    def content_type(self):
        return self._http.headers.get("Content-Type", "")

    def raw_payload(self) -> bytes:
        return self._http.content

    def _extra_metadata(self) -> dict:
        """Subclasses override to add extra fields. Default none

        Returns:
            dict: _description_
        """
        return {
            "content_type": self.content_type,
            "status_code": self.status_code,
            "response_type": type(self).__name__,
        }


class TransportErrorResponse(ArchivableEvent):
    def __init__(self, error_type: str, error_message: str) -> None:
        super().__init__()
        self.error_type = error_type
        self.error_message = error_message

    def _extra_metadata(self) -> dict:
        return super()._extra_metadata() | {
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


class ProtobufResponse(FeedResponse):
    def __init__(self, http_response, parsed: FeedMessage):
        super().__init__(http_response)
        self._parsed = parsed

    def parsed_message(self) -> FeedMessage:
        return self._parsed

    def _extra_metadata(self) -> dict:
        summary = summarize_feed(self._parsed)
        return super()._extra_metadata() | asdict(summary)


class ErrorResponse(FeedResponse):
    def raw_payload(self) -> None:
        return None

    def _extra_metadata(self) -> dict:
        return super()._extra_metadata() | {"error_body": self._http.text[:500]}


class UnknownResponse(FeedResponse):
    pass


def parse_response(http_response, expected_format: str | None = None) -> FeedResponse:
    if http_response.status_code >= 400:
        return ErrorResponse(http_response)

    body = http_response.content
    content_type = http_response.headers.get("Content-Type", "")

    if expected_format == "protobuf" or "protobuf" in content_type:
        try:
            parsed = FeedMessage.FromString(body)
            return ProtobufResponse(http_response, parsed=parsed)
        except DecodeError:
            pass  # not actually protobuf — fall through

    return UnknownResponse(http_response)
