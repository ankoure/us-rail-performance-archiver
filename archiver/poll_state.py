from dataclasses import dataclass
import dataclasses
import json
import os
import threading
from pathlib import Path
from urllib.parse import quote, unquote


@dataclass(frozen=True)
class PollState:
    etag: str | None
    last_modified: str | None
    last_digest: str | None


class PollStateStore:
    def __init__(self, path: str | None = None):
        self._store: dict[str, PollState] = {}
        self._lock = threading.Lock()
        self.path = (
            Path(path) if path else None
        )  # optional, for implementations that persist to disk
        if self.path:
            self.path.mkdir(parents=True, exist_ok=True)
            self._load()

    def get(self, feed_name: str) -> PollState:
        # Leaving unlocked as the worst that can happen is a poll that returns duplicate information
        return self._store.get(feed_name, PollState(None, None, None))

    def set(self, feed_name: str, state: PollState) -> None:
        with self._lock:
            self._store[feed_name] = state
            self._flush(feed_name)  # serialize + atomic write, still under the lock

    def _flush(self, feed_name: str) -> None:
        if not self.path:
            return
        file_path = self.path / f"{quote(feed_name, safe='')}.json"
        tmp = file_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(dataclasses.asdict(self._store[feed_name]), f)
        os.replace(tmp, file_path)

    def _load(self) -> None:
        if not self.path:
            return
        for file in self.path.glob("*.json"):
            with open(file) as f:
                self._store[unquote(file.stem)] = PollState(**json.load(f))
