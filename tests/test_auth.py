from archiver.auth import APIClient, APIKeyQueryAuth
import httpx
import pytest


def _apply(auth, url: str) -> httpx.URL:
    """Drive the httpx.Auth flow once and return the resulting request URL."""
    request = httpx.Request("GET", url)
    flow = auth.auth_flow(request)
    modified = next(flow)
    return modified.url


def test_no_existing_query_string():
    auth = APIKeyQueryAuth(key="secret123", param="api_key")
    url = _apply(auth, "https://api.example.com/data")
    assert str(url) == "https://api.example.com/data?api_key=secret123"


def test_existing_query_string_appends():
    auth = APIKeyQueryAuth(key="secret123", param="api_key")
    url = _apply(auth, "https://api.example.com/data?f=json")
    assert str(url) == "https://api.example.com/data?f=json&api_key=secret123"


@pytest.mark.parametrize(
    "key,expected_encoded",
    [
        ("secret/key", "secret%2Fkey"),
        ("secret+key", "secret%2Bkey"),
        ("secret/key+val", "secret%2Fkey%2Bval"),
    ],
)
def test_special_characters_encoded(key, expected_encoded):
    auth = APIKeyQueryAuth(key=key, param="api_key")
    url = _apply(auth, "https://api.example.com/data")
    assert str(url) == f"https://api.example.com/data?api_key={expected_encoded}"


def test_apiclient_sets_default_user_agent():
    client = APIClient("https://example.com")
    ua = client.client.headers["User-Agent"]
    assert "us-rail-archiver" in ua
    assert "github.com" in ua


def test_apiclient_merges_default_headers():
    client = APIClient(
        "https://example.com",
        default_headers={"accept-version": "3.0", "origin": "https://radar.mta.info"},
    )
    headers = client.client.headers
    # agency headers are present...
    assert headers["accept-version"] == "3.0"
    assert headers["origin"] == "https://radar.mta.info"
    # ...and the default User-Agent survives the merge (not replaced)
    assert "us-rail-archiver" in headers["User-Agent"]
