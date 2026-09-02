from datetime import datetime, timezone
from abc import ABC
import hashlib
from google.transit.gtfs_realtime_pb2 import FeedMessage


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
        self._digest: str | None = None

    @property
    def status_code(self):
        return self._http.status_code

    @property
    def content_type(self):
        return self._http.headers.get("Content-Type", "")

    def raw_payload(self) -> bytes:
        return self._http.content

    def content_digest(self) -> str:
        if self._digest is not None:
            return self._digest
        else:
            digest = hashlib.sha256(self._http.content).hexdigest()
            self._digest = digest
            return digest

    def _extra_metadata(self) -> dict:
        return {
            "content_type": self.content_type,
            "status_code": self.status_code,
            "response_type": type(self).__name__,
            "digest": self.content_digest(),
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


class JsonResponse(FeedResponse):
    def __init__(self, http_response, parsed: dict | list):
        super().__init__(http_response)
        self._parsed = parsed

    def parsed_payload(self) -> dict | list:
        return self._parsed


class ErrorResponse(FeedResponse):
    def raw_payload(self) -> None:
        return None

    def _extra_metadata(self) -> dict:
        return super()._extra_metadata() | {"error_body": self._http.text[:2000]}


class DuplicateResponse(FeedResponse):
    def raw_payload(self) -> None:
        return None

    def _extra_metadata(self) -> dict:
        return super()._extra_metadata()


class NotModifiedResponse(FeedResponse):
    def __init__(self, http_response, prior_digest):
        super().__init__(http_response)
        self._digest = prior_digest  # pre-seed the cache; content_digest() never touches the 304 body

    def raw_payload(self) -> None:
        return None


class UnknownResponse(FeedResponse):
    pass


class DecodeFailureResponse(FeedResponse):
    def __init__(self, http_response, drift) -> None:
        super().__init__(http_response)
        self._drift = drift

    def raw_payload(self) -> bytes:
        return self._http.content

    def _extra_metadata(self) -> dict:
        return super()._extra_metadata() | {
            "drift_missing_required": sorted(self._drift.missing_required),
            "drift_extras": sorted(self._drift.extras),
        }
