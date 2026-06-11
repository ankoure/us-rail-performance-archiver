from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from archiver.landing_layout import iter_window_objects, window_object_key
from archiver.logger import logger
from archiver.telemetry import Telemetry
from archiver.uploader import Uploader

# How long __aexit__ waits for the worker to finish its in-flight upload before
# giving up. The thread is a daemon, so process exit reaps a stuck one; any
# object it didn't ship is still on disk and ships on the next boot's scan.
_JOIN_TIMEOUT_S = 60.0


class LandingUploader:
    """Drains local landing-zone window objects to S3 on a background thread.

    Lifecycle is RAII via the async context-manager protocol so it nests in the
    poller's AsyncExitStack alongside the agency clients (see main.run). It owns
    exactly one worker thread for its whole lifetime.
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

    async def __aexit__(self, exc_type, exc, tb) -> None:
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
        """Ship every pending window object currently on disk, oldest first.

        `ignore_stop` is used only by the final shutdown drain, where _stop is
        already set but we still want one full (bounded) pass.
        """
        pending = self._pending()
        self._tel.gauge("landing.pending", len(pending))  # leading indicator
        for path in pending:
            if self._stop.is_set() and not ignore_stop:
                return  # bail fast on shutdown
            self._ship_one(path)

    def _pending(self) -> list[Path]:
        """Window objects awaiting shipment, oldest first by mtime.

        mtime ordering (rather than name) gives true oldest-first across the
        raw/ and metadata/ subtrees, which interleave within a window.
        """
        candidates = list(iter_window_objects(self._landing_dir))

        if not self._layout_checked:
            if candidates:
                self._layout_checked = True
            else:
                self._warn_if_layout_mismatch()

        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                # Vanished mid-scan (e.g. prune.py); sort last, _ship_one
                # tolerates the miss.
                return float("inf")

        return sorted(candidates, key=mtime)

    def _warn_if_layout_mismatch(self) -> None:
        """Detect stale globs: window-like files exist but no pattern matches.

        Guards against the silent failure where writer.py's layout drifts from
        _WINDOW_OBJECT_GLOBS  the gauge would read 0 pending and look healthy
        while nothing ships. armed until the first match; Runs on each empty scan until then
        bounded, because next(...) short-circuits at the first stray and it only runs while pending is empty.
        """
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
        """Upload one object; delete on success.

        On failure: log and RETURN (do not delete, do not raise)  the file
        stays on disk and the next scan retries. boto3 already retries
        transient blips; a persistent failure just grows the backlog, which the
        gauge and the disk alarm surface. Crucially, one bad object must not
        starve the rest of the scan.
        """
        key = self._key_for(path)
        try:
            self._uploader.upload(self._bucket, key, path)  # emits s3.request{op:put}
        except FileNotFoundError:
            return  # pruned out from under us between scan and ship; benign
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
        """Local file path -> S3 key (prefix + path relative to landing_dir).

        Mirrors how S3Sink keyed objects, so window keys are byte-identical to
        the old synchronous dual-write path  the Fargate rollup reads the same
        layout either way. (prefix is validated in __init__ to end with "/".)
        """
        return window_object_key(self._landing_dir, path, self._prefix)
