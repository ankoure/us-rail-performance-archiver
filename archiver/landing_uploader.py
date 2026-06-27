from __future__ import annotations

import asyncio
import itertools
import threading
import time
from pathlib import Path

from archiver.landing_layout import hour_s3_key, iter_window_objects, window_object_key
from archiver.logger import logger
from archiver.telemetry import Telemetry
from archiver.uploader import Uploader
from archiver.writer import merge_bins

# How long __aexit__ waits for the worker to finish its in-flight upload before
# giving up. The thread is a daemon, so process exit reaps a stuck one; any
# object it didn't ship is still on disk and ships on the next boot's scan.
_JOIN_TIMEOUT_S = 60.0

# Seconds after the hour boundary before we treat that hour as complete and
# merge it.  One extra window (300 s) gives the writer time to flush the last
# window of the hour before we read the files.
_HOUR_GRACE_S = 300.0


class LandingUploader:
    """Drains local landing-zone window objects to S3 on a background thread.

    Lifecycle is RAII via the async context-manager protocol so it nests in the
    poller's AsyncExitStack alongside the agency clients (see main.run). It owns
    exactly one worker thread for its whole lifetime.

    When ``merge_to_hourly=True``, instead of shipping each 5-minute window
    file individually the uploader waits until an hour is complete (wall-clock
    hour boundary + grace period), merges the 12 window ``.bin`` files into one
    ``hour=*.bin`` and similarly for ``.jsonl``, then ships just those two
    objects.  This reduces S3 PUT count by ~12× while keeping the 5-minute
    local flush for crash safety.
    """

    def __init__(
        self,
        landing_dir: str | Path,
        uploader: Uploader,
        bucket: str,
        prefix: str = "",
        *,
        telemetry: Telemetry,
        scan_interval: float = 30.0,
        merge_to_hourly: bool = False,
        feed_names: set[str] | None = None,
    ) -> None:
        if prefix and not prefix.endswith("/"):
            # Keys must stay byte-identical to the old synchronous S3Sink path;
            # a slash-less prefix would silently fuse onto the first key
            # segment ("data" + "raw/x.bin" -> "dataraw/x.bin"). Fail loudly at
            # construction instead of producing a misplaced object at runtime.
            raise ValueError(f"prefix must end with '/' (got {prefix!r})")

        self._landing_dir = Path(landing_dir)
        self._uploader = uploader
        self._bucket = bucket
        self._prefix = prefix
        self._tel = telemetry
        self._scan_interval = scan_interval
        self._merge_to_hourly = merge_to_hourly
        self._feed_names = feed_names

        # Set on shutdown. The worker waits on it WITH A TIMEOUT, so the same
        # primitive both paces scans and wakes the thread promptly to stop.
        self._stop = threading.Event()
        # armed until the first match; runs on each empty scan until then bounded,
        # because next(...) short-circuits at the first stray and it only runs while pending is empty.
        self._layout_checked = False
        # Created in __aenter__: threads are single-use, and creating it there
        # keeps __aexit__ safe to call even if entry never happened.
        self._thread: threading.Thread | None = None

    # --- lifecycle: async CM so it nests in the poller's AsyncExitStack ------

    async def __aenter__(self) -> "LandingUploader":
        self._thread = threading.Thread(
            target=self._run, name="landing-uploader", daemon=True
        )
        # The worker scans immediately before its first wait, so this start IS
        # the boot-recovery scan for anything left over from a previous run.
        self._thread.start()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self._stop.set()
        if self._thread is not None:
            # join() blocks, so offload it off the event loop. Bounded: a
            # worker stuck deep in boto3 retries must not hang shutdown it's
            # a daemon thread, and whatever it didn't ship is still on disk.
            await asyncio.to_thread(self._thread.join, _JOIN_TIMEOUT_S)
            if self._thread.is_alive():
                logger.warning(
                    "landing-uploader thread did not stop within %.0fs; "
                    "abandoning it (daemon). Pending objects ship next boot.",
                    _JOIN_TIMEOUT_S,
                )
                return  # don't run the drain concurrently with the worker

        # Final best-effort drain: the stack unwinds AFTER the loop's finally
        # (flush_all), so the last windows are already on disk  ship them now
        # so a clean shutdown leaves nothing behind. ignore_stop because _stop
        # is set; the pass is still bounded (one sweep of current files). Any
        # failure here is fine: the remainder ships on the next boot's scan.
        try:
            await asyncio.to_thread(self._scan_once, True)
        except Exception:
            logger.exception("final landing drain failed; remainder ships on next boot")

    # --- worker -------------------------------------------------------------

    def _run(self) -> None:
        """Worker-thread entrypoint: scan -> ship -> wait, until stopped."""
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:
                # One bad scan must never kill the thread.
                logger.exception("landing scan failed; retrying next interval")
            if self._stop.wait(timeout=self._scan_interval):
                break

    def _scan_once(self, ignore_stop: bool = False) -> None:
        if self._merge_to_hourly:
            self._scan_once_hourly(ignore_stop)
        else:
            self._scan_once_window(ignore_stop)

    # --- window (per-file) scan — original behaviour -----------------------

    def _scan_once_window(self, ignore_stop: bool = False) -> None:
        """Ship every pending window object currently on disk, oldest first."""
        pending = self._pending()
        self._tel.gauge("landing.pending", len(pending))
        for path in pending:
            if self._stop.is_set() and not ignore_stop:
                return
            self._ship_one(path)

    def _pending(self) -> list[Path]:
        """Window objects awaiting shipment, oldest first by mtime."""
        candidates = [
            p
            for p in iter_window_objects(self._landing_dir)
            if self._feed_names is None
            or p.relative_to(self._landing_dir).parts[0] in self._feed_names
        ]

        if not self._layout_checked:
            if candidates:
                self._layout_checked = True
            else:
                self._warn_if_layout_mismatch()

        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return float("inf")

        return sorted(candidates, key=mtime)

    def _warn_if_layout_mismatch(self) -> None:
        """Detect stale globs: window-like files exist but no pattern matches."""
        try:
            stray = next(
                (
                    p
                    for p in self._landing_dir.rglob("window=*")
                    if p.is_file() and p.suffix != ".tmp"
                ),
                None,
            )
        except OSError:
            return
        if stray is not None:
            logger.error(
                "landing layout mismatch: %s exists but matches no pattern in "
                "landing_layout.WINDOW_OBJECT_GLOBS - globs are likely stale vs "
                "writer.py; NOTHING will ship until they are fixed",
                stray,
            )

    def _ship_one(self, path: Path) -> None:
        """Upload one object; delete on success."""
        key = self._key_for(path)
        try:
            self._uploader.upload(self._bucket, key, path)
        except FileNotFoundError:
            return
        except Exception:
            logger.exception(
                "landing upload failed, will retry next scan: %s -> s3://%s/%s",
                path,
                self._bucket,
                key,
            )
            return
        path.unlink(missing_ok=True)

    def _key_for(self, path: Path) -> str:
        return window_object_key(self._landing_dir, path, self._prefix)

    # --- hourly-merge scan -------------------------------------------------

    def _scan_once_hourly(self, ignore_stop: bool = False) -> None:
        """Group completed-hour window files and merge-then-ship each group."""
        pending = self._pending()
        self._tel.gauge("landing.pending", len(pending))

        groups = self._group_by_hour(pending)
        now = time.time()
        for (feed, hour_unix), kinds in groups.items():
            if self._stop.is_set() and not ignore_stop:
                return
            hour_done = now > hour_unix + 3600 + _HOUR_GRACE_S
            if not hour_done and not ignore_stop:
                continue
            self._merge_and_ship(feed, hour_unix, kinds["bin"], kinds["jsonl"])

    def _group_by_hour(
        self, paths: list[Path]
    ) -> dict[tuple[str, int], dict[str, list[Path]]]:
        """Partition window files by (feed, hour_unix) and extension."""
        groups: dict[tuple[str, int], dict[str, list[Path]]] = {}
        for path in paths:
            rel = path.relative_to(self._landing_dir)
            feed = rel.parts[0]
            stem = path.stem  # "window=1719302400"
            if not stem.startswith("window="):
                continue
            window_unix = int(stem.split("=")[1])
            hour_unix = (window_unix // 3600) * 3600
            key = (feed, hour_unix)
            if key not in groups:
                groups[key] = {"bin": [], "jsonl": []}
            ext = path.suffix.lstrip(".")
            if ext in ("bin", "jsonl"):
                groups[key][ext].append(path)
        return groups

    def _merge_and_ship(
        self,
        feed: str,
        hour_unix: int,
        bin_paths: list[Path],
        jsonl_paths: list[Path],
    ) -> None:
        """Merge window files for one hour and upload as two hourly objects."""
        if not bin_paths and not jsonl_paths:
            return

        try:
            if bin_paths:
                merged_bin = merge_bins(bin_paths)
                bin_key = hour_s3_key(self._prefix, feed, hour_unix, "raw", "bin")
                self._uploader.put_bytes(self._bucket, bin_key, merged_bin)

            if jsonl_paths:
                sorted_jsonl = sorted(
                    jsonl_paths, key=lambda p: int(p.stem.split("=")[1])
                )
                merged_jsonl = b"".join(p.read_bytes() for p in sorted_jsonl)
                jsonl_key = hour_s3_key(
                    self._prefix, feed, hour_unix, "metadata", "jsonl"
                )
                self._uploader.put_bytes(self._bucket, jsonl_key, merged_jsonl)

        except Exception:
            logger.exception(
                "hourly merge upload failed: feed=%s hour=%d; will retry next scan",
                feed,
                hour_unix,
            )
            return

        for path in itertools.chain(bin_paths, jsonl_paths):
            path.unlink(missing_ok=True)
