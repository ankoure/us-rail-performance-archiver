from typing import Optional, Generator

import httpx

from archiver.rate_limit import NullRateLimiter, RateLimiter


class BearerAuth(httpx.Auth):
    def __init__(self, token: str):
        self.token = token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class APIKeyAuth(httpx.Auth):
    def __init__(self, key: str, header: str = "X-API-Key"):
        self.key = key
        self.header = header

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers[self.header] = self.key
        yield request


class APIKeyQueryAuth(httpx.Auth):
    def __init__(self, key: str, param: str):
        self.key = key
        self.param = param

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.url = request.url.copy_merge_params({self.param: self.key})
        yield request


class APIClient:
    DEFAULT_USER_AGENT = (
        "us-rail-archiver/0.1 "
        "(+https://github.com/ankoure/us-rail-performance-archiver; "
        "contact: andkoure2@gmail.com)"
    )

    def __init__(
        self,
        base_url: str,
        auth: Optional[httpx.Auth] = None,
        timeout: int = 5,
        limiter: RateLimiter | None = None,
        default_headers: dict[str, str] | None = None,
        verify: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Per-agency rate limiter (one client per agency). The poll loop consults
        # it as a non-blocking admission gate; None => unlimited.
        self.limiter = limiter or NullRateLimiter()
        headers = {"User-Agent": self.DEFAULT_USER_AGENT}
        if default_headers:
            headers.update(default_headers)

        self.client = httpx.AsyncClient(
            headers=headers, auth=auth, follow_redirects=True, verify=verify
        )

    def set_auth(self, auth: httpx.Auth):
        self.client.auth = auth

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        return await self.client.request(method, url, **kwargs)

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def aclose(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    @classmethod
    def with_basic(
        cls, base_url: str, username: str, password: str, **kwargs
    ) -> "APIClient":
        return cls(base_url, auth=httpx.BasicAuth(username, password), **kwargs)

    @classmethod
    def with_bearer(cls, base_url: str, token: str, **kwargs) -> "APIClient":
        return cls(base_url, auth=BearerAuth(token), **kwargs)

    @classmethod
    def with_api_key(
        cls, base_url: str, key: str, header: str = "X-API-Key", **kwargs
    ) -> "APIClient":
        return cls(base_url, auth=APIKeyAuth(key, header=header), **kwargs)

    @classmethod
    def with_api_key_query(
        cls, base_url: str, key: str, param: str, **kwargs
    ) -> "APIClient":
        return cls(base_url, auth=APIKeyQueryAuth(key, param=param), **kwargs)
