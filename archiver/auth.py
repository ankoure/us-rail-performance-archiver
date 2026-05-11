from typing import Optional

import requests
from requests.auth import AuthBase, HTTPBasicAuth
from urllib.parse import quote


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


class APIKeyQueryAuth(AuthBase):
    def __init__(self, key: str, param: str):
        self.key = key
        self.param = param

    def __call__(self, request):
        sep = "&" if "?" in request.url else "?"
        encoded_value = quote(self.key, safe="")
        request.url = f"{request.url}{sep}{self.param}={encoded_value}"
        return request


class APIClient:
    DEFAULT_USER_AGENT = (
        "us-rail-archiver/0.1 "
        "(+https://github.com/ankoure/us-rail-peformance-archiver; "
        "contact: andkoure2@gmail.com)"
    )

    def __init__(
        self,
        base_url: str,
        auth: Optional[AuthBase] = None,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.DEFAULT_USER_AGENT
        self.session.auth = auth

    def set_auth(self, auth: AuthBase):
        """Swap auth at runtime."""
        self.session.auth = auth

    def request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method, url, **kwargs)
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
    def with_api_key(cls, base_url: str, key: str, header: str = "X-API-Key", **kwargs):
        return cls(base_url, auth=APIKeyAuth(key, header=header), **kwargs)

    @classmethod
    def with_api_key_query(cls, base_url: str, key: str, param: str, **kwargs):
        return cls(base_url, auth=APIKeyQueryAuth(key, param=param), **kwargs)
