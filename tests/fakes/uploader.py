from dataclasses import dataclass
from pathlib import Path


@dataclass
class UploadCall:
    bucket: str
    key: str
    bytes: bytes
    storage_class: str | None


class FakeUploader:
    """Records upload calls; lets tests pre-seed 'existing' keys."""

    def __init__(self) -> None:
        self.uploads: list[UploadCall] = []
        self._existing: set[tuple[str, str]] = set()
        # Pre-seeded readable objects (e.g. an S3 landing zone) for the read side
        # (list_keys/get_bytes) used by S3Source. Keyed by S3 key; bucket-agnostic.
        self.store: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.store[key] = data

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        return [k for k in self.store if k.startswith(prefix)]

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self.store[key]

    def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self._existing

    def upload(
        self,
        bucket: str,
        key: str,
        local_path: Path,
        *,
        storage_class: str | None = None,
    ) -> None:
        data = local_path.read_bytes()
        self.uploads.append(UploadCall(bucket, key, data, storage_class))
        self._existing.add((bucket, key))

    def mark_existing(self, bucket: str, key: str) -> None:
        """Pre-seed a key as if it already lives in S3 (without uploading)."""
        self._existing.add((bucket, key))
