from abc import ABC
from datetime import timezone, datetime
import hashlib
import io
from pathlib import Path
import struct
import threading
from typing import Iterable

from archiver.response import FeedResponse
from archiver.logger import logger
import json
from abc import abstractmethod

from archiver.sink import Sink


class BaseWriter(ABC):
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)

    def write_bytes_atomic(self, path: Path, data: bytes) -> None:
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(data)
        tmp_path.rename(path)

    def append_metadata(self, feed_name: str, response: FeedResponse) -> None:
        date = response.get_datetime()
        metadata_path = (
            self.base_dir
            / feed_name
            / "metadata"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
            / "data.jsonl"
        )
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        record = response.to_metadata_row()
        with metadata_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    @abstractmethod
    def write(self, feed_name: str, response: FeedResponse) -> None: ...

    @abstractmethod
    def flush_due(self, now: float) -> None: ...

    @abstractmethod
    def flush_all(self) -> None: ...


class LocalWriter(BaseWriter):
    def __init__(self, base_dir: str) -> None:
        super().__init__(base_dir)

    def write(self, feed_name: str, response: FeedResponse) -> None:
        date = response.get_datetime()
        file_path = (
            self.base_dir
            / feed_name
            / "raw"
            / f"year={date.year}"
            / f"month={date.month}"
            / f"day={date.day}"
            / f"{response.get_timestamp()}.bin"
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)

        payload = response.raw_payload()
        if payload is not None:
            self.write_bytes_atomic(file_path, payload)
        else:
            logger.info("No content to persist")

        self.append_metadata(feed_name, response)

    def flush_due(self, now: float) -> None:
        pass

    def flush_all(self) -> None:
        pass


MAGIC = b"\x89GRT"
VERSION = 0x01
HEADER = MAGIC + bytes([VERSION])


def merge_bins(paths: Iterable[Path]) -> bytes:
    """Concatenate frames from multiple window .bin files into one.

    Writes one header then appends the raw frame bytes from each file in
    ascending window-timestamp order (oldest frames first).  The caller is
    responsible for ensuring all paths share the same feed and hour bucket.
    """
    buf = io.BytesIO()
    buf.write(HEADER)
    for path in sorted(paths, key=lambda p: int(p.stem.split("=")[1])):
        data = path.read_bytes()
        buf.write(data[5:])  # skip the per-file 5-byte header
    return buf.getvalue()


class FrameError(Exception):
    """Base class for all frame errors. Catching this means the object is untrustworthy."""


class BadHeaderError(FrameError):
    """Magic bytes or version number did not match. The whole file is suspect."""


class TruncatedFrameError(FrameError):
    """A short read occurred mid-frame. The write was likely interrupted."""


class CorruptFrameError(FrameError):
    """Digest mismatch. The payload does not match its stored digest."""

    def __init__(self, stored: bytes, computed: bytes):
        self.stored = stored
        self.computed = computed
        super().__init__(
            f"Digest mismatch: stored={stored.hex()}, computed={computed.hex()}"
        )


class FrameWriter:
    def __init__(self, stream):
        self._stream = stream
        self._stream.write(HEADER)

    def write_frame(self, payload, digest):
        if len(digest) != 32:
            raise ValueError(f"digest must be 32 bytes, got {len(digest)}")
        self._stream.write(struct.pack(">I", len(payload)))
        self._stream.write(digest)
        self._stream.write(payload)


class FrameReader:
    def __init__(self, stream):
        self._stream = stream
        self._read_header()

    def _read_header(self):
        header = self._stream.read(5)
        if len(header) < 5:
            raise EOFError("Stream too short to contain a header")
        magic, version = header[:4], header[4]
        if magic != MAGIC:
            raise BadHeaderError(f"Bad magic: expected {MAGIC!r}, got {magic!r}")
        if version != VERSION:
            raise BadHeaderError(f"Unsupported version: 0x{version:02x}")

    def _read_exact(self, n):
        buf = self._stream.read(n)
        if len(buf) == 0:
            return None
        if len(buf) < n:
            raise TruncatedFrameError(
                f"Truncated stream: wanted {n} bytes, got {len(buf)}"
            )
        return buf

    def _read_frame(self):
        raw_len = self._read_exact(4)
        if raw_len is None:
            return None

        (payload_len,) = struct.unpack(">I", raw_len)

        digest = self._read_exact(32)
        if digest is None:
            raise EOFError("Truncated stream: missing digest")

        payload = self._read_exact(payload_len)
        if payload is None:
            raise EOFError("Truncated stream: missing payload")

        actual = hashlib.sha256(payload).digest()
        if actual != digest:
            raise CorruptFrameError(stored=digest, computed=actual)

        return payload, digest

    def __iter__(self):
        while True:
            result = self._read_frame()
            if result is None:
                return
            yield result


class BatchingWriter(BaseWriter):
    def __init__(self, base_dir: str, sink: Sink, window_seconds: int = 300) -> None:
        super().__init__(base_dir)
        self._window_seconds = window_seconds
        self._buffer: dict[tuple[str, int], dict[str, bytes]] = {}
        self._meta_buffer: dict[tuple[str, int], list[dict]] = {}
        self._lock = threading.Lock()
        self._sink = sink

    def write(self, feed_name: str, response: FeedResponse) -> None:
        self.append_metadata(feed_name, response)  # local daily jsonl — unchanged

        window = int(response.get_timestamp() // self._window_seconds)
        key = (feed_name, window)
        row = response.to_metadata_row()
        payload = response.raw_payload()

        with self._lock:
            self._meta_buffer.setdefault(key, []).append(
                row
            )  # EVERY poll, incl. 304/dup
            if payload is not None:
                self._buffer.setdefault(key, {})[response.content_digest()] = payload

    def flush_due(self, now: float) -> None:
        current = int(now // self._window_seconds)
        self._flush(lambda window: window < current)  # closed = strictly older windows

    def flush_all(self) -> None:
        self._flush(lambda window: True)  # shutdown: everything is "closed"

    def _flush(self, is_closed) -> None:
        # one lock acquisition → both buffers pop consistently; release before any I/O
        with self._lock:
            bins = {
                k: self._buffer.pop(k) for k in list(self._buffer) if is_closed(k[1])
            }
            metas = {
                k: self._meta_buffer.pop(k)
                for k in list(self._meta_buffer)
                if is_closed(k[1])
            }
        self._write_buckets(bins, metas)

    def _write_buckets(self, bins: dict, metas: dict) -> None:
        # INVARIANT: metas.keys() ⊇ bins.keys() — every poll appends a metadata row
        # (before the payload early-return), so any window with a payload also has
        # metadata. All-304 windows are in metas but NOT bins. So iterate metas.
        for key, rows in metas.items():
            feed, window = key
            if key in bins:
                self._put_bin(feed, window, bins[key])  # bin FIRST...
            self._put_metadata(feed, window, rows)  # ...then metadata

    def _window_key(self, feed: str, window: int, kind: str, ext: str) -> str:
        unix = window * self._window_seconds
        dt = datetime.fromtimestamp(unix, tz=timezone.utc)
        return (
            f"{feed}/{kind}/year={dt.year}/month={dt.month}"
            f"/day={dt.day}/window={unix}.{ext}"
        )

    def _put_bin(self, feed: str, window: int, frames: dict[str, bytes]) -> None:
        buf = io.BytesIO()
        fw = FrameWriter(buf)
        for digest_hex, payload in frames.items():
            fw.write_frame(payload, bytes.fromhex(digest_hex))
        self._sink.put(self._window_key(feed, window, "raw", "bin"), buf.getvalue())

    def _put_metadata(self, feed: str, window: int, rows: list[dict]) -> None:
        body = "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")
        self._sink.put(self._window_key(feed, window, "metadata", "jsonl"), body)
