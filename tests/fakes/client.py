from tests.conftest import _FakeResponse


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []  # optional: record headers to assert later

    async def get(self, path, headers=None):
        self.calls.append(headers)
        return self._response  # same content every call -> same digest


class _ConditionalFakeClient:
    def __init__(
        self, content: bytes, etag: str | None = None, last_modified: str | None = None
    ):
        self.content = content
        self.etag = etag
        self.last_modified = last_modified
        self.calls = []  # optional: record headers to assert later

    async def get(self, path, headers=None):
        headers = headers or {}  # archive_one passes {} when empty
        self.calls.append(headers)
        if self.etag and headers.get("If-None-Match") == self.etag:
            return _FakeResponse(304, headers={"ETag": self.etag})
        if (
            self.last_modified
            and headers.get("If-Modified-Since") == self.last_modified
        ):
            return _FakeResponse(304, headers={"Last-Modified": self.last_modified})
        return _FakeResponse(
            200,
            headers={"ETag": self.etag, "Last-Modified": self.last_modified},
            content=self.content,
        )


class _SequenceFakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, path, headers=None):
        self.calls.append(headers)
        return self._responses.pop(0)
