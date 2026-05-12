from dataclasses import dataclass
from archiver.auth import APIClient
from archiver.parser import Parser
from archiver.decoder import Decoder


@dataclass
class Feed:
    name: str  # "kcm-realtime", "mta-nyct-subway-vehicles"
    path: str  # path under the client's base_url
    client: APIClient  # which transport to use (shared across feeds with same auth)
    parser: Parser  # resolved at config-load from expected_format
    decoder: Decoder  # resolved at config-load from decoder name
