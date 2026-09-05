import io
import itertools
import shutil
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from archiver.source import Source, S3Source
from archiver.uploader import Uploader
from archiver.telemetry import Telemetry, NoOpTelemetry
from archiver.logger import logger


class Shipper:
    _COLD_STORAGE_CLASS = "DEEP_ARCHIVE"

    def __init__(
        self,
        source: Source,
        curated_dir: Path,
        uploader: Uploader,
        cold_bucket: str,
        hot_bucket: str,
        cold_prefix: str = "",
        hot_prefix: str = "",
        telemetry: Telemetry | None = None,
        *,
        feed_names: Iterable[str] = (),
        feed_agency: dict[str, str] | None = None,
        feed_alerts_capable: Iterable[str] = (),
        landing_dir: Path | None = None,
        landing_bucket: str | None = None,
        landing_prefix: str = "",
    ) -> None:
        # Landing reads (discovery + the cold tarball's raw/metadata) go through
        # the Source seam, so ship works whether landing is local (on-box) or in
        # S3 (Fargate) — the same seam the rollup uses. curated_dir stays local:
        # the rollup writes parquet there in the same container before ship runs.
        self.source = source
        self.curated_dir = curated_dir
        self.uploader = uploader
        self.cold_bucket = cold_bucket
        self.hot_bucket = hot_bucket
        self.cold_prefix = cold_prefix
        self.hot_prefix = hot_prefix
        self.telemetry = telemetry or NoOpTelemetry()
        # When ship is called without a specific --feed, discover narrows S3 list
        # calls to one feed's day-prefix at a time instead of listing the whole
        # landing; that needs the configured feed list.
        self.feed_names = list(feed_names)
        self.feed_agency: dict[str, str] = feed_agency or {}
        # Feeds whose decoder can produce AlertRow -- i.e. the ones snapshot.py
        # actually processes (see pipeline/snapshot.py's own identical filter).
        # prune_s3 uses this to know which feeds should have a snapshot object
        # before their landing raw data is deleted; a feed that never produces
        # alerts should not be held hostage waiting for one.
        self.feed_alerts_capable: frozenset[str] = frozenset(feed_alerts_capable)
        # Local-only; used by prune (which deletes the on-box landing tree). None
        # on Fargate, where prune isn't run (S3 landing expires via lifecycle).
        self.landing_dir = landing_dir
        self.landing_bucket = landing_bucket
        self.landing_prefix = landing_prefix

    def run(
        self,
        feed=None,
        day=None,
        *,
        force=False,
        hot_only=False,
        cold_only=False,
        workers=4,
    ):
        pairs = list(self._discover(feed, day))
        if not pairs:
            return
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(
                    self.ship_one,
                    fn,
                    d,
                    force=force,
                    hot_only=hot_only,
                    cold_only=cold_only,
                ): (fn, d)
                for fn, d in pairs
            }
            for fut in as_completed(futures):
                fn, d = futures[fut]
                try:
                    fut.result()
                except Exception:
                    logger.exception("ship failed: %s/%s", fn, d)

    def ship_one(
        self,
        feed_name: str,
        day: date,
        *,
        force: bool = False,
        hot_only: bool = False,
        cold_only: bool = False,
    ) -> None:
        """Ship one (feed, day). `hot_only` and `cold_only` are mirrors of each
        other and mutually exclusive: the cold tarball is built from landing
        alone, while hot parquet and snapshots come from the curated tree the
        rollup/gold/snapshot steps write. Splitting them lets the batch archive
        landing BEFORE running any of those steps -- see pipeline/agency_batch.py.
        """
        if not hot_only:
            self._ship_cold(feed_name, day, force=force)
        if cold_only:
            return
        self._ship_hot(feed_name, day, force=force)
        self._ship_snapshots(feed_name, day, force=force)

    def _ship_cold(self, feed_name, day, *, force):
        key = self._cold_key(feed_name, day)
        tags = {"feed": feed_name, "agency": self.feed_agency.get(feed_name, "unknown")}
        if not force and self.uploader.exists(self.cold_bucket, key):
            self.telemetry.incr("ship.cold.skipped", tags=tags)
            logger.debug("cold already exists, skipping: %s/%s", self.cold_bucket, key)
            return

        with self._build_tarball(feed_name, day) as tar_path:
            with self.telemetry.span("ship.cold", tags=tags):
                self.uploader.upload(
                    self.cold_bucket,
                    key,
                    tar_path,
                    storage_class=self._COLD_STORAGE_CLASS,
                )
            self.telemetry.histogram(
                "ship.cold.bytes",
                tar_path.stat().st_size,
                tags=tags,
            )

    def _ship_hot(self, feed_name, day, *, force):
        agency = self.feed_agency.get(feed_name, "unknown")
        parquets = itertools.chain(
            self._curated_parquets(feed_name, day),
            self._curated_version_parquets(feed_name),
        )
        for parquet in parquets:
            parts = parquet.relative_to(self.curated_dir).parts
            # First path segment is <kind>; gold marts nest one deeper
            # (metrics/stop_day, metrics/route_day) so keep both for telemetry.
            kind = "/".join(parts[:2]) if parts[0] == "metrics" else parts[0]
            key = self._hot_key(parquet)
            tags = {"feed": feed_name, "kind": kind, "agency": agency}

            if not force and self.uploader.exists(self.hot_bucket, key):
                self.telemetry.incr("ship.hot.skipped", tags=tags)
                logger.debug(
                    "hot already exists, skipping: %s/%s", self.hot_bucket, key
                )
                continue

            with self.telemetry.span("ship.hot", tags=tags):
                self.uploader.upload(self.hot_bucket, key, parquet)
            self.telemetry.histogram(
                "ship.hot.bytes",
                parquet.stat().st_size,
                tags=tags,
            )

    @contextmanager
    def _build_tarball(self, feed_name: str, day: date):
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tar_path = Path(tmp.name)
        try:
            # compresslevel=6 (not tarfile's default 9): on the 2-vCPU box the daily
            # ship saturated both cores at gzip-9, the slowest level. Level 6 is ~2-3x
            # faster for a few % larger tarball — negligible in DEEP_ARCHIVE, where these
            # cold tarballs land. CPU is the box's scarce resource, storage is not.
            base = f"year={day.year}/month={day.month}/day={day.day}"
            with tarfile.open(tar_path, "w:gz", compresslevel=6) as tar:
                # Pull raw + metadata objects through the Source (local files or S3
                # objects) and add each under its raw/<part> | metadata/<part>
                # arcname, preserving the on-box tarball layout so a restore is
                # backend-agnostic.
                for sub, items in (
                    ("raw", self.source.iter_bins(feed_name, day)),
                    ("metadata", self.source.iter_metadata(feed_name, day)),
                ):
                    for name, data in items:
                        info = tarfile.TarInfo(name=f"{sub}/{base}/{name}")
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            yield tar_path
        finally:
            tar_path.unlink(missing_ok=True)

    def _cold_key(self, feed_name: str, day: date) -> str:
        return f"{self.cold_prefix}{feed_name}/year={day.year}/month={day.month}/day={day.day}.tar.gz"

    def _hot_key(self, parquet: Path) -> str:
        rel = parquet.relative_to(self.curated_dir).as_posix()
        return f"{self.hot_prefix}{rel}"

    def _snapshot_key(self, feed_name: str, day: date) -> str:
        # Mirrors _curated_snapshots' partition shape (hardcoding "alerts" the
        # same way that glob's own comment does -- one kind exists today).
        return (
            f"{self.hot_prefix}snapshots/alerts/feed={feed_name}/"
            f"year={day.year}/month={day.month}/day={day.day}/data.json.gz"
        )

    def _curated_parquets(self, feed_name: str, day: date) -> Iterator[Path]:
        # Silver datasets live one segment above feed= (e.g. vehicles/feed=...);
        # the gold marts live two segments above (metrics/stop_day/feed=...).
        # Glob both layouts explicitly rather than with ** to avoid matching any
        # deeper, unintended trees.
        partition = f"feed={feed_name}/year={day.year}/month={day.month}/day={day.day}/data.parquet"
        yield from self.curated_dir.glob(f"*/{partition}")
        yield from self.curated_dir.glob(f"metrics/*/{partition}")

    def _curated_version_parquets(self, feed_name: str) -> Iterator[Path]:
        # Version-partitioned marts (gtfs_stops, gtfs_shapes, etc. — see
        # docs/design/static-gtfs-normalization.md) are keyed by (feed,
        # version_slug), not (feed, day), so they don't fit the day-partition
        # glob _curated_parquets uses. The S3 key derived from this path has no
        # day component either, so _ship_hot's existing exists() check already
        # skips a version once any prior run has shipped it — no separate
        # "already shipped" tracking needed here. Wildcarding the mart name
        # (not naming gtfs_stops/gtfs_calendar/etc. explicitly) means any future
        # mart adopting this same layout ships for free.
        yield from self.curated_dir.glob(
            f"metrics/*/feed={feed_name}/version=*/data.parquet"
        )

    def _curated_snapshots(self, feed_name: str, day: date) -> Iterator[Path]:
        # Snapshots only have one kind today (alerts), but the directory already
        # treats <kind> as a segment, same as gold marts under metrics/. Wildcard
        # it now rather than hardcoding "alerts", for the same reason gold marts
        # wildcard the mart name in _curated_parquets: cheap now, avoids a second
        # edit here when a second snapshot kind shows up.
        partition = f"feed={feed_name}/year={day.year}/month={day.month}/day={day.day}/data.json.gz"
        yield from self.curated_dir.glob(f"snapshots/*/{partition}")

    def _ship_snapshots(self, feed_name: str, day: date, *, force: bool):
        agency = self.feed_agency.get(feed_name, "unknown")
        for snapshot in self._curated_snapshots(feed_name, day):
            parts = snapshot.relative_to(self.curated_dir).parts
            # Every snapshot path is snapshots/<kind>/feed=.../... — unlike
            # _curated_parquets, there's only one glob shape feeding this, so
            # no branch is needed to disambiguate depth.
            kind = "/".join(parts[:2])
            key = self._hot_key(snapshot)
            tags = {"feed": feed_name, "kind": kind, "agency": agency}

            if not force and self.uploader.exists(self.hot_bucket, key):
                self.telemetry.incr("ship.snapshot.skipped", tags=tags)
                logger.debug(
                    "snapshot already exists, skipping: %s/%s", self.hot_bucket, key
                )
                continue

            with self.telemetry.span("ship.snapshot", tags=tags):
                self.uploader.upload(self.hot_bucket, key, snapshot)
            self.telemetry.histogram(
                "ship.snapshot.bytes",
                snapshot.stat().st_size,
                tags=tags,
            )

    def _discover(self, feed=None, day=None):
        # Discovery goes through the Source (local or S3). Iterate one feed at a
        # time so the S3 backend lists a single feed's day-prefix per call rather
        # than the whole landing; when --feed is given, just that one.
        today = datetime.now(tz=timezone.utc).date()
        feeds = [feed] if feed is not None else self.feed_names
        seen: set[tuple[str, date]] = set()
        for feed_name in feeds:
            for fn, partition_day in self.source.discover(feed_name, day):
                if partition_day >= today:  # today is still being written
                    continue
                if (fn, partition_day) in seen:
                    continue
                seen.add((fn, partition_day))
                yield fn, partition_day

    def _discover_partitions(self) -> set[tuple[str, date]]:
        """Every (feed, day) raw/metadata day-partition currently on disk."""
        found: set[tuple[str, date]] = set()
        if self.landing_dir is None:
            return found
        for sub in ("raw", "metadata"):
            for p in self.landing_dir.glob(f"*/{sub}/year=*/month=*/day=*"):
                rel = p.relative_to(self.landing_dir).parts
                try:
                    found.add(
                        (
                            rel[0],
                            date(
                                int(rel[2].removeprefix("year=")),
                                int(rel[3].removeprefix("month=")),
                                int(rel[4].removeprefix("day=")),
                            ),
                        )
                    )
                except (ValueError, IndexError):
                    continue
        return found

    def prune(
        self, *, keep_days: int = 3, day: date | None = None, dry_run: bool = False
    ) -> dict[str, int]:
        """Delete landing-zone raw+metadata day-partitions older than keep_days.

        SAFETY: a day is deleted only if its cold tarball is confirmed in S3
        (same exists() check ship uses). A day not yet shipped is skipped, never
        deleted — so this is crash-safe and idempotent. `keep_days` retains that
        many recent days as a buffer for re-rollups; `day` restricts to one day;
        `dry_run` logs what it would delete without touching disk.
        """
        if self.landing_dir is None:
            raise RuntimeError(
                "prune requires a local landing_dir; the S3 landing is expired by "
                "an S3 lifecycle rule, not by prune."
            )
        cutoff = datetime.now(tz=timezone.utc).date() - timedelta(days=keep_days)
        deleted = skipped = 0
        for feed_name, partition_day in sorted(self._discover_partitions()):
            if partition_day >= cutoff or (day is not None and partition_day != day):
                continue
            key = self._cold_key(feed_name, partition_day)
            if not self.uploader.exists(self.cold_bucket, key):
                logger.warning(
                    "prune skip %s %s: cold tarball %s not in s3 (not shipped yet)",
                    feed_name,
                    partition_day,
                    key,
                )
                self.telemetry.incr("prune.skipped_unshipped", tags={"feed": feed_name})
                skipped += 1
                continue
            for sub in ("raw", "metadata"):
                d = (
                    self.landing_dir
                    / feed_name
                    / sub
                    / f"year={partition_day.year}"
                    / f"month={partition_day.month}"
                    / f"day={partition_day.day}"
                )
                if d.exists():
                    logger.info("%sprune %s", "[dry-run] " if dry_run else "", d)
                    if not dry_run:
                        shutil.rmtree(d)
            if not dry_run:
                self.telemetry.incr("prune.deleted", tags={"feed": feed_name})
            deleted += 1
        logger.info(
            "prune: %d day-partition(s) %s, %d skipped (unshipped)",
            deleted,
            "would be deleted" if dry_run else "deleted",
            skipped,
        )
        return {"deleted": deleted, "skipped": skipped}

    def prune_s3(
        self, *, keep_days: int = 3, day: date | None = None, dry_run: bool = False
    ) -> dict[str, int]:
        """
        Delete landing-zone raw+metadata day-partitions older than keep_days.

        SAFETY: a day is deleted only if its cold tarball is confirmed in S3
        (same exists() check ship uses), AND -- for a feed whose decoder can
        produce alerts -- its snapshot object is also confirmed in S3. Cold-ship
        now runs before snapshot in the nightly chain (see agency_batch.py's
        run_agency docstring), so the cold tarball existing is no longer proof
        that snapshot ever got to this day. Without the second check, a feed
        that OOMs in snapshot.py every night (see local.heavy_agencies in
        terraform/rollup.tf) would still have its landing data pruned once the
        day crosses keep_days, permanently losing snapshot's only input for a
        day it never actually processed. A day not yet shipped, or not yet
        snapshotted, is skipped, never deleted -- so this is crash-safe and
        idempotent. `keep_days` retains that many recent days as a buffer for
        re-rollups; `day` restricts to one day; `dry_run` logs what it would
        delete without touching disk.
        """
        if self.landing_bucket is None:
            raise RuntimeError("S3 prune requires a staging bucket to be set ...")
        s3_source = S3Source(self.uploader, self.landing_bucket, self.landing_prefix)
        cutoff = datetime.now(tz=timezone.utc).date() - timedelta(days=keep_days)
        deleted = skipped = 0
        for feed_name, partition_day in sorted(s3_source.discover()):
            try:
                if partition_day >= cutoff or (
                    day is not None and partition_day != day
                ):
                    continue
                key = self._cold_key(feed_name, partition_day)
                if not self.uploader.exists(self.cold_bucket, key):
                    logger.warning(
                        "prune skip %s %s: cold tarball %s not in s3 (not shipped yet)",
                        feed_name,
                        partition_day,
                        key,
                    )
                    self.telemetry.incr(
                        "prune.skipped_unshipped", tags={"feed": feed_name}
                    )
                    skipped += 1
                    continue
                if feed_name in self.feed_alerts_capable:
                    snapshot_key = self._snapshot_key(feed_name, partition_day)
                    if not self.uploader.exists(self.hot_bucket, snapshot_key):
                        logger.warning(
                            "prune skip %s %s: no alert snapshot %s in s3 "
                            "(snapshot.py hasn't succeeded for this day yet)",
                            feed_name,
                            partition_day,
                            snapshot_key,
                        )
                        self.telemetry.incr(
                            "prune.skipped_no_snapshot", tags={"feed": feed_name}
                        )
                        skipped += 1
                        continue
                for sub in ("raw", "metadata"):
                    prefix = f"{self.landing_prefix}{feed_name}/{sub}/year={partition_day.year}/month={partition_day.month}/day={partition_day.day}/"
                    keys = list(self.uploader.list_keys(self.landing_bucket, prefix))
                    if dry_run:
                        logger.info(
                            "[dry-run] prune_s3 would delete %d keys under %s",
                            len(keys),
                            prefix,
                        )
                    else:
                        self.uploader.delete_objects(self.landing_bucket, keys)

                if not dry_run:
                    self.telemetry.incr("prune.deleted", tags={"feed": feed_name})
                deleted += 1
            except Exception as e:
                logger.error("prune_s3 failed: %s %s: %s", feed_name, partition_day, e)

        logger.info(
            "prune: %d day-partition(s) %s, %d skipped (unshipped)",
            deleted,
            "would be deleted" if dry_run else "deleted",
            skipped,
        )
        return {"deleted": deleted, "skipped": skipped}
