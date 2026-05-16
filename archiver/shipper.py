import tarfile
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from archiver.uploader import Uploader
from archiver.telemetry import Telemetry, NoOpTelemetry
from archiver.logger import logger


class Shipper:
    _COLD_STORAGE_CLASS = "DEEP_ARCHIVE"

    def __init__(
        self,
        landing_dir: Path,
        curated_dir: Path,
        uploader: Uploader,
        cold_bucket: str,
        hot_bucket: str,
        cold_prefix: str = "",
        hot_prefix: str = "",
        telemetry: Telemetry | None = None,
    ) -> None:
        self.landing_dir = landing_dir
        self.curated_dir = curated_dir
        self.uploader = uploader
        self.cold_bucket = cold_bucket
        self.hot_bucket = hot_bucket
        self.cold_prefix = cold_prefix
        self.hot_prefix = hot_prefix
        self.telemetry = telemetry or NoOpTelemetry()

    def run(
        self, feed: str | None = None, day: date | None = None, *, force: bool = False
    ) -> None:
        for feed_name, partition_day in self._discover(feed, day):
            self.ship_one(feed_name, partition_day, force=force)

    def ship_one(self, feed_name: str, day: date, *, force: bool = False) -> None:
        self._ship_cold(feed_name, day, force=force)
        self._ship_hot(feed_name, day, force=force)

    def _ship_cold(self, feed_name, day, *, force):
        key = self._cold_key(feed_name, day)
        if not force and self.uploader.exists(self.cold_bucket, key):
            self.telemetry.incr("ship.cold.skipped", tags={"feed": feed_name})
            logger.debug("cold already exists, skipping: %s/%s", self.cold_bucket, key)
            return

        with self._build_tarball(feed_name, day) as tar_path:
            with self.telemetry.span(
                "ship.cold",
                resource=feed_name,
                tags={"feed": feed_name, "day": day.isoformat()},
            ):
                self.uploader.upload(
                    self.cold_bucket,
                    key,
                    tar_path,
                    storage_class=self._COLD_STORAGE_CLASS,
                )
            self.telemetry.histogram(
                "ship.cold.bytes",
                tar_path.stat().st_size,
                tags={"feed": feed_name},
            )

    def _ship_hot(self, feed_name, day, *, force):
        for parquet in self._curated_parquets(feed_name, day):
            kind = parquet.relative_to(self.curated_dir).parts[
                0
            ]  # first path segment is <kind>
            key = self._hot_key(parquet)

            if not force and self.uploader.exists(self.hot_bucket, key):
                self.telemetry.incr(
                    "ship.hot.skipped", tags={"feed": feed_name, "kind": kind}
                )
                logger.debug(
                    "hot already exists, skipping: %s/%s", self.hot_bucket, key
                )
                continue

            with self.telemetry.span(
                "ship.hot",
                resource=feed_name,
                tags={"feed": feed_name, "day": day.isoformat(), "kind": kind},
            ):
                self.uploader.upload(self.hot_bucket, key, parquet)
            self.telemetry.histogram(
                "ship.hot.bytes",
                parquet.stat().st_size,
                tags={"feed": feed_name, "kind": kind},
            )

    @contextmanager
    def _build_tarball(self, feed_name: str, day: date):
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tar_path = Path(tmp.name)
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                for sub in ("raw", "metadata"):
                    src = (
                        self.landing_dir
                        / feed_name
                        / sub
                        / f"year={day.year}"
                        / f"month={day.month}"
                        / f"day={day.day}"
                    )
                    if src.exists():
                        tar.add(
                            src,
                            arcname=f"{sub}/year={day.year}/month={day.month}/day={day.day}",
                        )
            yield tar_path
        finally:
            tar_path.unlink(missing_ok=True)

    def _cold_key(self, feed_name: str, day: date) -> str:
        return f"{self.cold_prefix}{feed_name}/year={day.year}/month={day.month}/day={day.day}.tar.gz"

    def _hot_key(self, parquet: Path) -> str:
        rel = parquet.relative_to(self.curated_dir).as_posix()
        return f"{self.hot_prefix}{rel}"

    def _curated_parquets(self, feed_name: str, day: date) -> Iterator[Path]:
        pattern = f"*/feed={feed_name}/year={day.year}/month={day.month}/day={day.day}/data.parquet"
        yield from self.curated_dir.glob(pattern)

    def _discover(self, feed=None, day=None):
        today = datetime.now(tz=timezone.utc).date()
        feed_glob = feed if feed else "*"
        for metadata_file in self.landing_dir.glob(
            f"{feed_glob}/metadata/year=*/month=*/day=*/data.jsonl"
        ):
            rel_parts = metadata_file.relative_to(self.landing_dir).parts
            # rel_parts: <feed>/metadata/year=Y/month=M/day=D/data.jsonl
            try:
                feed_name = rel_parts[0]
                partition_day = date(
                    int(rel_parts[2].removeprefix("year=")),
                    int(rel_parts[3].removeprefix("month=")),
                    int(rel_parts[4].removeprefix("day=")),
                )
            except (ValueError, IndexError):
                continue
            if partition_day >= today:
                continue
            if day is not None and partition_day != day:
                continue
            yield feed_name, partition_day

            today = datetime.now(tz=timezone.utc).date()
            feed_glob = feed if feed else "*"
