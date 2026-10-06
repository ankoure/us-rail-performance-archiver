import io
import tarfile
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Protocol
import json

import zstandard

from archiver.logger import logger


class Source(Protocol):
    def discover(
        self, feed: str | None = None, day: date | None = None
    ) -> Iterator[tuple[str, date]]: ...
    def read_metadata(
        self, feed: str, day: date
    ) -> bytes: ...  # day's jsonl, repeated polls removed; b"" if absent
    def iter_bins(
        self, feed: str, day: date
    ) -> Iterator[tuple[str, bytes]]: ...  # (name, bytes)
    def iter_metadata(
        self, feed: str, day: date
    ) -> Iterator[tuple[str, bytes]]: ...  # (name, bytes) per metadata file


def select_metadata_names(names: Iterable[str]) -> list[str]:
    """Pick which of a day's metadata `.jsonl` files make up its poll log.

    A day dir can hold the append-only daily `data.jsonl` (box-local, the
    whole day), `window=*.jsonl` slices re-serialized from it for shipping,
    and legacy `hour=*.jsonl`. When `data.jsonl` is present it is a superset
    of the slices beside it, so reading both would double every poll.

    Takes bare file names (callers strip their own Path / S3 key) and returns
    the selected names in the order to concatenate them. read_metadata
    applies this; iter_metadata deliberately does not — the cold tarball
    keeps every file, and restores re-apply the selection on read.
    """
    names = list(names)
    if "data.jsonl" in names:
        return ["data.jsonl"]
    return sorted(names)


def _poll_key(line: bytes) -> tuple[int, str | None] | None:
    """(timestamp in integer µs, digest) for a poll row; None if it has no key."""
    try:
        row = json.loads(line)
        micros = round(float(row["timestamp"]) * 1_000_000)
        digest = row.get("digest")
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return None
    if digest is not None and not isinstance(digest, str):
        return None
    return micros, digest


def dedupe_metadata_lines(chunks: Iterable[bytes]) -> bytes:
    """Join a day's selected metadata files, dropping repeated poll rows.

    Rows are the same poll when (timestamp to the µs, digest) match; the
    first occurrence's original bytes are kept. Lines with no key (malformed,
    or no timestamp) pass through untouched: dropping data is the caller's
    decision, not this layer's.
    """
    seen: set[tuple[int, str | None]] = set()
    out: list[bytes] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.strip():
                continue
            key = _poll_key(line)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            out.append(line + b"\n")
    return b"".join(out)


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
            seen: set[date] = set()  # per feed; ~24 window files share one day
            for jsonl_path in metadata_dir.rglob("*.jsonl"):
                # Directory parts only, relative to metadata_dir: the file
                # name itself is now "window=....jsonl", which the old
                # all-parts parse would feed to int() and blow up on.
                partitions = {}
                for part in jsonl_path.relative_to(metadata_dir).parent.parts:
                    k, sep, v = part.partition("=")
                    if sep:
                        partitions[k] = v
                try:
                    partition_day = date(
                        int(partitions["year"]),
                        int(partitions["month"]),
                        int(partitions["day"]),
                    )
                except (KeyError, ValueError):
                    continue  # not a year=/month=/day= layout; skip
                if day is not None and partition_day != day:
                    continue
                if partition_day in seen:
                    continue
                seen.add(partition_day)
                yield feed_name, partition_day

    def read_metadata(self, feed: str, day: date) -> bytes:
        """Read the full metadata jsonl for the given feed and day, or b"" if absent."""
        day_dir = (
            self.landing_dir
            / feed
            / "metadata"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
        )
        names = select_metadata_names(p.name for p in day_dir.glob("*.jsonl"))
        return dedupe_metadata_lines((day_dir / name).read_bytes() for name in names)

    def iter_bins(self, feed: str, day: date) -> Iterator[tuple[str, bytes]]:
        """Yield (bin_name, bytes) for every bin for the given feed and day.

        No tar branch here on purpose: the window tars only ever exist inside
        LandingUploader's scratch directory, which is torn down on every exit
        path. What is on the local disk is always loose {digest}.bin files.
        """
        bin_files = (self.landing_dir / feed / "raw").glob(
            f"year={day.year}/month={day.month}/day={day.day}/*.bin"
        )
        for path in bin_files:
            yield (path.name, path.read_bytes())

    def iter_metadata(self, feed: str, day: date) -> Iterator[tuple[str, bytes]]:
        """Yield (name, bytes) for every metadata file (data.jsonl and/or
        window=*.jsonl) for the given feed and day — the per-file form the cold
        tarball needs, vs read_metadata's concatenated stream the rollup needs."""
        meta_files = (self.landing_dir / feed / "metadata").glob(
            f"year={day.year}/month={day.month}/day={day.day}/*.jsonl"
        )
        for path in meta_files:
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
        """Yield (name, bytes) for every payload in the day's raw prefix.

        Four object shapes coexist and all come out of one call: legacy flat
        `.bin` objects from before the migration, yielded as-is; legacy
        uncompressed `.tar` objects (pre-gzip _ship_raw_window); legacy
        `.tar.gz` objects (pre-zstd); and current `.tar.zst` objects — the
        last three all unpacked into their `{digest}.bin` members. Callers
        see the same (name, bytes) contract either way —
        payloads.iter_payloads dispatches on the name.

        Dispatch is by suffix, longest-first where they nest: `.tar.zst` and
        `.tar.gz` are tested before the bare `.tar` branch, which would
        otherwise never be reached by those keys anyway (neither ends in
        ".tar") but the ordering documents the intent.
        """
        prefix = self._day_prefix(feed, "raw", day)
        seen_members: set[str] = set()

        for key in self._list_keys(self._bucket, prefix):
            if key.endswith(".bin"):
                name = key.rsplit("/", 1)[-1]  # window=*.bin — keep the ext
                data = self._uploader.get_bytes(self._bucket, key)
                yield name, data

            elif key.endswith(".tar.zst"):
                data = self._uploader.get_bytes(self._bucket, key)
                # zstd can't be named in a tarfile mode string (native
                # "r:zst" is 3.14+), so the decompressor wraps the buffer
                # and tarfile reads a plain tar out of it. Stream mode
                # ("r|") because stream_reader isn't seekable — and it
                # keeps the DECOMPRESSED tar out of memory, which matters
                # more here than in the .gz branch: these are the current
                # objects and the largest.
                dctx = zstandard.ZstdDecompressor()
                with dctx.stream_reader(io.BytesIO(data)) as reader:
                    yield from self._iter_tar_members(reader, "r|", key, seen_members)

            elif key.endswith(".tar.gz"):
                data = self._uploader.get_bytes(self._bucket, key)
                # mode="r:gz" is exact-match gzip, matching the
                # tarfile.open(staged, "w:gz") that wrote these — same
                # "loud failure on mismatch" reasoning as the plain ".tar"
                # branch below.
                yield from self._iter_tar_members(
                    io.BytesIO(data), "r:gz", key, seen_members
                )

            elif key.endswith(".tar"):
                data = self._uploader.get_bytes(self._bucket, key)
                # mode="r:" is exact-match uncompressed, matching the
                # tarfile.open(staged, "w") that wrote these (pre-gzip
                # objects still in S3 from before the .tar.gz switch).
                # "r"/"r:*" would auto-detect and silently accept a gzip'd
                # tar that has no business being in this path; a loud
                # ReadError is the point.
                yield from self._iter_tar_members(
                    io.BytesIO(data), "r:", key, seen_members
                )

    def _iter_tar_members(self, fileobj, mode, key, seen_members):
        """Yield (name, bytes) for each flat file member of one tar.

        Takes a FILEOBJ, not bytes: the zstd branch has a stream_reader to
        hand over, and wrapping bytes in io.BytesIO at the other call sites
        is cheaper than forcing a decompress-to-bytes there.
        """
        try:
            with tarfile.open(fileobj=fileobj, mode=mode) as tar:
                for member in tar:
                    if not member.isfile():
                        continue  # dir/link entries carry no payload

                    # arcname was path.name, so a member name should be
                    # a bare "{digest}.bin". Anything with a path in it
                    # didn't come from _ship_raw_window; nothing is
                    # written to disk here so it can't traverse, but it
                    # would break the digest join downstream.
                    name = member.name
                    if "/" in name or name in (".", ".."):
                        logger.warning(
                            "Unexpected non-flat member %r in %s; skipping",
                            name,
                            key,
                        )
                        continue

                    # Retried ships produce a second tar (window_tar_key
                    # stamps int(time.time()), so the key differs) with
                    # overlapping members. Names are content digests, so
                    # a repeat is the same bytes by construction and
                    # dropping it is safe — yielding it twice would
                    # double-count the poll in the rollup.
                    if name in seen_members:
                        continue

                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    payload = extracted.read()  # may raise; name not yet claimed
                    seen_members.add(name)
                    yield name, payload

        # ZstdError alongside TarError: a truncated or corrupt zstd frame
        # surfaces as the former, and it has to be skipped-and-logged like
        # any other unreadable object rather than killing the run.
        except (tarfile.TarError, zstandard.ZstdError):
            logger.exception(
                "Unreadable raw tar %s; skipping the remainder of the object "
                "(its payloads are lost for this run — the local {digest}.bin "
                "files were deleted once the upload was confirmed). Under the "
                "streaming modes any members already yielded above are kept.",
                key,
            )
            return

    def read_metadata(self, feed, day):
        prefix = self._day_prefix(feed, "metadata", day)
        by_name = {
            k.rsplit("/", 1)[-1]: k
            for k in self._list_keys(self._bucket, prefix)
            if k.endswith(".jsonl")
        }
        return dedupe_metadata_lines(
            self._uploader.get_bytes(self._bucket, by_name[name])
            for name in select_metadata_names(by_name)
        )

    def iter_metadata(self, feed, day):
        prefix = self._day_prefix(feed, "metadata", day)
        for key in self._list_keys(self._bucket, prefix):
            if key.endswith(".jsonl"):
                name = key.rsplit("/", 1)[-1]  # window=*.jsonl — keep the ext
                yield name, self._uploader.get_bytes(self._bucket, key)
