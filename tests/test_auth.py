from archiver.auth import APIKeyQueryAuth
import pytest
from unittest.mock import Mock


def test_no_existing_query_string():
    auth = APIKeyQueryAuth(key="secret123", param="api_key")
    mock_request = Mock()
    mock_request.url = "https://api.example.com/data"

    result = auth(mock_request)  # invoking __call__

    assert result.url == "https://api.example.com/data?api_key=secret123"


def test_existing_query_string_appends():
    auth = APIKeyQueryAuth(key="secret123", param="api_key")
    mock_request = Mock()
    mock_request.url = "https://api.example.com/data?f=json"

    result = auth(mock_request)

    assert result.url == "https://api.example.com/data?f=json&api_key=secret123"


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
    mock_request = Mock()
    mock_request.url = "https://api.example.com/data"

    auth(mock_request)

    assert (
        mock_request.url == f"https://api.example.com/data?api_key={expected_encoded}"
    )
