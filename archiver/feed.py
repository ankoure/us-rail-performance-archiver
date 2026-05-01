from dataclasses import dataclass
from archiver.auth import APIClient


@dataclass
class Feed:
    name: str  # "kcm-realtime", "mta-nyct-subway-vehicles"
    path: str  # path under the client's base_url
    client: APIClient  # which transport to use (shared across feeds with same auth)
    expected_format: str = "protobuf"  # or "json", or "auto"
    # later: parser config, expected entity types, unmix rules
