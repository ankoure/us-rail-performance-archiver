from pathlib import Path
from typing import Protocol

from archiver.uploader import Uploader


class Sink(Protocol):
    def put(self, key: str, data: bytes) -> None: ...


class LocalSink:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def put(self, key: str, data: bytes) -> None:
        path = self.base_dir / key
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(data)
        tmp_path.rename(path)


class TeeSink:
    def __init__(self, sinks: list[Sink]) -> None:
        self._sinks = sinks

    def put(self, key: str, data: bytes) -> None:
        for sink in self._sinks:
            sink.put(key, data)


class S3Sink:
    def __init__(self, uploader: Uploader, bucket: str, prefix: str = "") -> None:
        self._uploader = uploader
        self._bucket = bucket
        self._prefix = prefix

    def put(self, key: str, data: bytes) -> None:
        self._uploader.put_bytes(self._bucket, self._prefix + key, data)
