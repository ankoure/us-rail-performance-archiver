from typing import Optional

import requests
from requests.auth import AuthBase, HTTPBasicAuth


class BearerAuth(AuthBase):
    def __init__(self, token: str):
        self.token = token

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self.token}"
        return request


class APIKeyAuth(AuthBase):
    def __init__(self, key: str, header: str = "X-API-Key"):
        self.key = key
        self.header = header

    def __call__(self, request):
        request.headers[self.header] = self.key
        return request


class APIClient:
    def __init__(
        self,
        base_url: str,
        auth: Optional[AuthBase] = None,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = auth

    def set_auth(self, auth: AuthBase):
        """Swap auth at runtime."""
        self.session.auth = auth

    def request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    @classmethod
    def with_basic(cls, base_url: str, username: str, password: str, **kwargs):
        return cls(base_url, auth=HTTPBasicAuth(username, password), **kwargs)

    @classmethod
    def with_bearer(cls, base_url: str, token: str, **kwargs):
        return cls(base_url, auth=BearerAuth(token), **kwargs)

    @classmethod
    def with_api_key(cls, base_url: str, key: str, **kwargs):
        return cls(base_url, auth=APIKeyAuth(key), **kwargs)
