from abc import ABC
from datetime import timezone, datetime
import hashlib
import io
from pathlib import Path
import struct
import threading

from archiver.response import FeedResponse
from archiver.logger import logger
import json
from abc import abstractmethod


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
    def __init__(self, base_dir: str, window_seconds: int = 300) -> None:
        super().__init__(base_dir)
        self._window_seconds = window_seconds
        self._buffer: dict[tuple[str, int], dict[str, bytes]] = {}
        self._lock = threading.Lock()

    def write(self, feed_name: str, response: FeedResponse) -> None:
        self.append_metadata(feed_name, response)

        payload = response.raw_payload()
        if payload is None:
            return

        digest = response.content_digest()
        window = int(response.get_timestamp() // self._window_seconds)
        key = (feed_name, window)

        with self._lock:
            self._buffer.setdefault(key, {})[digest] = payload

    def flush_due(self, now: float) -> None:
        current = int(now // self._window_seconds)

        with self._lock:
            closed = {
                k: self._buffer.pop(k) for k in list(self._buffer) if k[1] < current
            }
        self._write_buckets(closed)

    def flush_all(self) -> None:
        with self._lock:
            closed = {
                k: self._buffer.pop(k) for k in list(self._buffer)
            }  # no window filter
        self._write_buckets(closed)

    def _write_buckets(self, closed: dict) -> None:
        for (feed, window), frames in closed.items():
            buf = io.BytesIO()
            writer = FrameWriter(buf)
            for digest_hex, payload in frames.items():
                writer.write_frame(payload, bytes.fromhex(digest_hex))

            window_dt = datetime.fromtimestamp(
                window * self._window_seconds, tz=timezone.utc
            )
            window_path = (
                self.base_dir
                / feed
                / "raw"
                / f"year={window_dt.year}"
                / f"month={window_dt.month}"
                / f"day={window_dt.day}"
                / f"window={window * self._window_seconds}.bin"
            )
            self.write_bytes_atomic(window_path, buf.getvalue())
