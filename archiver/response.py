import requests
from datetime import datetime, timezone
from abc import ABC


class FeedResponse(ABC):
    def __init__(self, http_response):
        self._http = http_response  # private, by convention

    @property
    def status_code(self):
        return self._http.status_code

    @property
    def content_type(self):
        return self._http.headers.get("Content-Type", "")

    def raw_payload(self) -> bytes:
        return self._http.content

    def to_metadata_row(self) -> dict:
        return {
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "content_type": self.content_type,
            "status_code": self.status_code,
            **self._extra_metadata(),
        }

    def _extra_metadata(self) -> dict:
        """Subclasses override to add extra fields. Default none

        Returns:
            dict: _description_
        """
        return {}


class ProtobufResponse(FeedResponse):
    def raw_payload(self) -> bytes:
        return self._http.content

    def _extra_metadata(self) -> dict:
        # TODO: Add metadata for Vehicle, Service Alert and Trip Update counts
        return {}


class ErrorResponse(FeedResponse):
    def _extra_metadata(self) -> dict:
        return {"error_body": self._http.text[:500]}


class UnknownResponse(FeedResponse):
    pass


def parse_response(http_response: requests.Response) -> FeedResponse:
    content_type = http_response.headers.get("Content-Type", "")
    if "protobuf" in content_type:
        return ProtobufResponse(http_response)
    if http_response.status_code >= 400:
        return ErrorResponse(http_response)
    return UnknownResponse(http_response)
