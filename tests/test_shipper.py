import io
import tarfile
from datetime import date, datetime, timezone

import pytest

from archiver.shipper import Shipper
from tests.fakes.uploader import FakeUploader


FEED = "fake-feed"
DAY = date(2026, 5, 1)


@pytest.fixture
def dirs(tmp_path):
    Y, M, D = DAY.year, DAY.month, DAY.day

    raw_dir = (
        tmp_path / "landing" / FEED / "raw" / f"year={Y}" / f"month={M}" / f"day={D}"
    )
    raw_dir.mkdir(parents=True)
    (raw_dir / "123.bin").write_bytes(b"raw-payload")

    meta_dir = (
        tmp_path
        / "landing"
        / FEED
        / "metadata"
        / f"year={Y}"
        / f"month={M}"
        / f"day={D}"
    )
    meta_dir.mkdir(parents=True)
    (meta_dir / "data.jsonl").write_text('{"ts": 123}\n')

    parquet_dir = (
        tmp_path
        / "curated"
        / "vehicles"
        / f"feed={FEED}"
        / f"year={Y}"
        / f"month={M}"
        / f"day={D}"
    )
    parquet_dir.mkdir(parents=True)
    (parquet_dir / "data.parquet").write_bytes(b"PAR1fake")

    return tmp_path


@pytest.fixture
def shipper(dirs):
    return Shipper(
        landing_dir=dirs / "landing",
        curated_dir=dirs / "curated",
        uploader=FakeUploader(),
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
    )


def test_ship_one_cold_upload(shipper):
    shipper.ship_one(FEED, DAY)
    uploader = shipper.uploader

    cold = [u for u in uploader.uploads if u.bucket == "cold-bucket"]
    assert len(cold) == 1

    c = cold[0]
    assert c.storage_class == "DEEP_ARCHIVE"
    assert c.key == shipper._cold_key(FEED, DAY)
    assert c.key == "archive/fake-feed/year=2026/month=5/day=1.tar.gz"
    assert c.bytes[:2] == b"\x1f\x8b", "cold upload is not a gzip stream"

    with tarfile.open(fileobj=io.BytesIO(c.bytes), mode="r:gz") as tar:
        names = tar.getnames()
    assert any("raw/" in n for n in names), f"raw subtree missing from tarball: {names}"
    assert any("metadata/" in n for n in names), (
        f"metadata subtree missing from tarball: {names}"
    )


def test_ship_one_hot_upload(shipper):
    shipper.ship_one(FEED, DAY)
    uploader = shipper.uploader

    hot = [u for u in uploader.uploads if u.bucket == "hot-bucket"]
    assert len(hot) == 1

    h = hot[0]
    assert h.storage_class is None
    assert (
        h.key == "curated/vehicles/feed=fake-feed/year=2026/month=5/day=1/data.parquet"
    )


# --- prune ---------------------------------------------------------------- #
def _raw_dir(shipper, feed, day):
    return (
        shipper.landing_dir
        / feed
        / "raw"
        / f"year={day.year}"
        / f"month={day.month}"
        / f"day={day.day}"
    )


def test_prune_deletes_shipped_old_day(shipper):
    # DAY (2026-05-01) is well past keep_days. Mark its cold tarball as already in S3.
    shipper.uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, DAY))

    result = shipper.prune(keep_days=3)

    assert result == {"deleted": 1, "skipped": 0}
    assert not _raw_dir(shipper, FEED, DAY).exists()
    meta_day = (
        shipper.landing_dir
        / FEED
        / "metadata"
        / f"year={DAY.year}"
        / f"month={DAY.month}"
        / f"day={DAY.day}"
    )
    assert not meta_day.exists(), "metadata day-partition not pruned"


def test_prune_skips_unshipped_day(shipper):
    # Cold tarball NOT seeded -> prune must NOT delete (crash/loss safety).
    result = shipper.prune(keep_days=3)

    assert result == {"deleted": 0, "skipped": 1}
    assert _raw_dir(shipper, FEED, DAY).exists(), "deleted raw that wasn't shipped!"


def test_prune_dry_run_touches_nothing(shipper):
    shipper.uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, DAY))

    result = shipper.prune(keep_days=3, dry_run=True)

    assert result == {"deleted": 1, "skipped": 0}  # would-be count
    assert _raw_dir(shipper, FEED, DAY).exists(), "dry-run deleted from disk"


def test_prune_keeps_recent_days(shipper):
    # A partition within keep_days must survive even when shipped.
    today = datetime.now(tz=timezone.utc).date()
    recent = _raw_dir(shipper, FEED, today)
    recent.mkdir(parents=True)
    (recent / "1.bin").write_bytes(b"x")
    shipper.uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, today))

    shipper.prune(keep_days=3)

    assert recent.exists(), "pruned a day inside the keep_days buffer"


def test_ship_one_skips_when_keys_exist(dirs):
    uploader = FakeUploader()
    shipper = Shipper(
        landing_dir=dirs / "landing",
        curated_dir=dirs / "curated",
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
    )

    uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, DAY))
    uploader.mark_existing(
        "hot-bucket",
        "curated/vehicles/feed=fake-feed/year=2026/month=5/day=1/data.parquet",
    )

    shipper.ship_one(FEED, DAY)

    assert uploader.uploads == []


def test_ship_one_force_bypasses_skip(dirs):
    uploader = FakeUploader()
    shipper = Shipper(
        landing_dir=dirs / "landing",
        curated_dir=dirs / "curated",
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
    )

    uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, DAY))
    uploader.mark_existing(
        "hot-bucket",
        "curated/vehicles/feed=fake-feed/year=2026/month=5/day=1/data.parquet",
    )

    shipper.ship_one(FEED, DAY, force=True)

    cold = [u for u in uploader.uploads if u.bucket == "cold-bucket"]
    hot = [u for u in uploader.uploads if u.bucket == "hot-bucket"]
    assert len(cold) == 1
    assert len(hot) == 1


def test_ship_one_hot_only_skips_cold(shipper):
    shipper.ship_one(FEED, DAY, hot_only=True)
    uploader = shipper.uploader

    assert [u for u in uploader.uploads if u.bucket == "cold-bucket"] == []
    hot = [u for u in uploader.uploads if u.bucket == "hot-bucket"]
    assert len(hot) == 1


def test_ship_one_hot_only_with_force_still_skips_cold(dirs):
    uploader = FakeUploader()
    shipper = Shipper(
        landing_dir=dirs / "landing",
        curated_dir=dirs / "curated",
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
    )
    uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, DAY))
    uploader.mark_existing(
        "hot-bucket",
        "curated/vehicles/feed=fake-feed/year=2026/month=5/day=1/data.parquet",
    )

    shipper.ship_one(FEED, DAY, force=True, hot_only=True)

    assert [u for u in uploader.uploads if u.bucket == "cold-bucket"] == []
    hot = [u for u in uploader.uploads if u.bucket == "hot-bucket"]
    assert len(hot) == 1


def test_discover_filters_today_and_future(tmp_path):
    today = datetime.now(tz=timezone.utc).date()
    for day in (date(2020, 1, 1), today):
        d = (
            tmp_path
            / FEED
            / "metadata"
            / f"year={day.year}"
            / f"month={day.month}"
            / f"day={day.day}"
        )
        d.mkdir(parents=True)
        (d / "data.jsonl").write_text("")

    shipper = Shipper(
        landing_dir=tmp_path,
        curated_dir=tmp_path,
        uploader=FakeUploader(),
        cold_bucket="c",
        hot_bucket="h",
    )
    assert list(shipper._discover()) == [(FEED, date(2020, 1, 1))]
