from datetime import date
from pathlib import Path
from typing import Iterator, Protocol


class Source(Protocol):
    def discover(
        self, feed: str | None = None, day: date | None = None
    ) -> Iterator[tuple[str, date]]: ...
    def read_metadata(
        self, feed: str, day: date
    ) -> bytes: ...  # day's full jsonl; b"" if absent
    def iter_bins(
        self, feed: str, day: date
    ) -> Iterator[tuple[str, bytes]]: ...  # (name, bytes)


class LocalSource:
    def __init__(self, landing_dir: str | Path):
        self.landing_dir = Path(landing_dir)

    def discover(
        self, feed: str | None = None, day: date | None = None
    ) -> Iterator[tuple[str, date]]:
        """Yield (feed_name, day) for every metadata partition"""
        for metadata_dir in self.landing_dir.glob("*/metadata"):
            feed_name = metadata_dir.parent.name
            if feed is not None and feed_name != feed:
                continue
            for jsonl_path in metadata_dir.rglob("data.jsonl"):
                partitions = {
                    part.split("=")[0]: int(part.split("=")[1])
                    for part in jsonl_path.parts
                    if "=" in part
                }
                partition_day = date(
                    partitions["year"], partitions["month"], partitions["day"]
                )
                if day is not None and partition_day != day:
                    continue
                yield feed_name, partition_day

    def read_metadata(self, feed: str, day: date) -> bytes:
        """Read the full metadata jsonl for the given feed and day, or b"" if absent."""
        path = (
            self.landing_dir
            / feed
            / "metadata"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
            / "data.jsonl"
        )
        return path.read_bytes() if path.exists() else b""

    def iter_bins(self, feed: str, day: date) -> Iterator[tuple[str, bytes]]:
        """Yield (bin_name, bytes) for every bin for the given feed and day."""
        bin_files = (self.landing_dir / feed / "raw").glob(
            f"year={day.year}/month={day.month}/day={day.day}/*.bin"
        )
        for path in bin_files:
            yield (path.name, path.read_bytes())


class S3Source:
    def __init__(self, uploader, bucket, prefix=""):
        self._uploader, self._bucket, self._prefix = uploader, bucket, prefix

    def _list_keys(self, bucket, prefix):
        return self._uploader.list_keys(bucket, prefix)

    def _day_prefix(self, feed, kind, day):
        return (
            self._prefix
            + f"{feed}/{kind}/year={day.year}/month={day.month}/day={day.day}/"
        )

    def discover(self, feed=None, day=None):
        # Optional optimization: when both are pinned, list only that day's
        # metadata subtree. The exact filters below still run, so correctness
        # never depends on this narrowing.
        if feed is not None and day is not None:
            prefix = self._day_prefix(feed, "metadata", day)
        else:
            prefix = self._prefix

        found = set()
        for key in self._list_keys(self._bucket, prefix):
            if "/metadata/" not in key:
                continue
            rel = key[len(self._prefix) :]
            f = rel.split("/")[0]
            kv = {}
            for seg in rel.split("/"):
                k, sep, v = seg.partition("=")
                if sep:
                    kv[k] = v
            try:
                d = date(int(kv["year"]), int(kv["month"]), int(kv["day"]))
            except (KeyError, ValueError):
                continue  # not a year=/month=/day= layout; skip
            found.add((f, d))  # many window=*.jsonl collapse to one (feed, day)

        if feed is not None:
            found = {(f, d) for (f, d) in found if f == feed}
        if day is not None:
            found = {(f, d) for (f, d) in found if d == day}
        return found

    def iter_bins(self, feed, day):
        prefix = self._day_prefix(feed, "raw", day)
        for key in self._list_keys(self._bucket, prefix):
            if key.endswith(".bin"):
                name = key.rsplit("/", 1)[-1]  # window=*.bin — keep the ext
                data = self._uploader.get_bytes(self._bucket, key)
                yield name, data

    def read_metadata(self, feed, day):
        prefix = self._day_prefix(feed, "metadata", day)
        keys = sorted(
            k for k in self._list_keys(self._bucket, prefix) if k.endswith(".jsonl")
        )
        chunks = [self._uploader.get_bytes(self._bucket, k) for k in keys]
        return b"".join(chunks)
