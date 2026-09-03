from .auth import APIClient, BearerAuth, APIKeyAuth
from .feed import Feed
from .response import FeedResponse
from .parser import parse_response
from .archiver import FeedArchiver

# Side-effect import: registers TfnswDecoder in Decoder._registry. Decoders
# defined inside decoder.py register when that module loads, but TfNSW lives in
# its own module (it has its own row schema), so something has to import it.
# Here, because importing any `archiver.*` submodule runs this __init__ first --
# so both archiver/loader.py and pipeline/gold.py get registration for free.
from . import tfnsw_decoder  # noqa: F401

__all__ = [
    "APIClient",
    "BearerAuth",
    "APIKeyAuth",
    "Feed",
    "FeedResponse",
    "parse_response",
    "FeedArchiver",
]
