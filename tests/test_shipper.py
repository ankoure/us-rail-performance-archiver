import io
import tarfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from archiver.shipper import Shipper
from archiver.source import LocalSource, S3Source
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
        source=LocalSource(dirs / "landing"),
        curated_dir=dirs / "curated",
        uploader=FakeUploader(),
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
        feed_names=[FEED],
        landing_dir=dirs / "landing",
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


def test_ship_one_hot_upload_includes_gold_marts(dirs, shipper):
    """The gold marts nest two segments above feed= (metrics/stop_day/feed=...);
    the hot glob must discover them, not just the one-segment silver layout."""
    part = f"year={DAY.year}/month={DAY.month}/day={DAY.day}"
    for mart in ("stop_day", "route_day"):
        mart_dir = dirs / "curated" / "metrics" / mart / f"feed={FEED}" / part
        mart_dir.mkdir(parents=True)
        (mart_dir / "data.parquet").write_bytes(b"PAR1fake")

    shipper.ship_one(FEED, DAY)
    hot_keys = {u.key for u in shipper.uploader.uploads if u.bucket == "hot-bucket"}

    assert f"curated/metrics/stop_day/feed={FEED}/{part}/data.parquet" in hot_keys
    assert f"curated/metrics/route_day/feed={FEED}/{part}/data.parquet" in hot_keys
    # the original silver parquet still ships too
    assert f"curated/vehicles/feed={FEED}/{part}/data.parquet" in hot_keys


def test_ship_one_hot_upload_includes_version_partitioned_marts(dirs, shipper):
    """Regression: pipeline/gtfs.py's marts (gtfs_stops, gtfs_shapes, etc. —
    see docs/design/static-gtfs-normalization.md) are keyed by (feed,
    version_slug), not (feed, day). A first version of this shipping fix only
    handled the day-partition glob shape and silently never uploaded these —
    computed on Fargate's ephemeral disk, then discarded when the task exited."""
    version = "20260521T005740"
    for mart in ("gtfs_stops", "gtfs_shapes"):
        mart_dir = (
            dirs / "curated" / "metrics" / mart / f"feed={FEED}" / f"version={version}"
        )
        mart_dir.mkdir(parents=True)
        (mart_dir / "data.parquet").write_bytes(b"PAR1fake")

    shipper.ship_one(FEED, DAY)
    hot_keys = {u.key for u in shipper.uploader.uploads if u.bucket == "hot-bucket"}

    assert (
        f"curated/metrics/gtfs_stops/feed={FEED}/version={version}/data.parquet"
        in hot_keys
    )
    assert (
        f"curated/metrics/gtfs_shapes/feed={FEED}/version={version}/data.parquet"
        in hot_keys
    )
    # the day-partitioned silver parquet still ships too
    part = f"year={DAY.year}/month={DAY.month}/day={DAY.day}"
    assert f"curated/vehicles/feed={FEED}/{part}/data.parquet" in hot_keys


def test_ship_one_version_partitioned_mart_skipped_when_already_shipped(dirs, shipper):
    """The S3 key for a version-partitioned mart has no day component, so once
    any prior day's run has shipped a given (feed, version_slug), a later run
    that recomputes the same version locally must not re-upload it."""
    version = "20260521T005740"
    mart_dir = (
        dirs
        / "curated"
        / "metrics"
        / "gtfs_stops"
        / f"feed={FEED}"
        / f"version={version}"
    )
    mart_dir.mkdir(parents=True)
    (mart_dir / "data.parquet").write_bytes(b"PAR1fake")

    key = f"curated/metrics/gtfs_stops/feed={FEED}/version={version}/data.parquet"
    shipper.uploader.mark_existing("hot-bucket", key)

    shipper.ship_one(FEED, DAY)
    hot_keys = [u.key for u in shipper.uploader.uploads if u.bucket == "hot-bucket"]

    assert key not in hot_keys


# --- S3 landing source (the Fargate path) --------------------------------- #
def test_ship_one_cold_and_hot_from_s3_source(tmp_path):
    """Regression: on Fargate the landing lives in S3, not on local disk. Ship
    must discover + build the cold tarball through the Source (S3), and upload
    the locally-rolled-up curated parquet to hot."""
    part = f"year={DAY.year}/month={DAY.month}/day={DAY.day}"
    up = FakeUploader()
    # Seed an S3 landing zone (windowed format) for FEED/DAY.
    up.put(f"{FEED}/raw/{part}/window=100.bin", b"raw-A")
    up.put(f"{FEED}/raw/{part}/window=200.bin", b"raw-B")
    up.put(f"{FEED}/metadata/{part}/window=100.jsonl", b'{"ts": 1}\n')
    # Curated parquet is local (the rollup writes it before ship runs).
    pq_dir = tmp_path / "curated" / "vehicles" / f"feed={FEED}" / part
    pq_dir.mkdir(parents=True)
    (pq_dir / "data.parquet").write_bytes(b"PAR1fake")

    shipper = Shipper(
        source=S3Source(up, "landing-bucket", ""),
        curated_dir=tmp_path / "curated",
        uploader=up,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
        feed_names=[FEED],
        # landing_dir intentionally omitted — none exists on Fargate.
    )

    shipper.ship_one(FEED, DAY)

    cold = [u for u in up.uploads if u.bucket == "cold-bucket"]
    assert len(cold) == 1
    assert cold[0].storage_class == "DEEP_ARCHIVE"
    with tarfile.open(fileobj=io.BytesIO(cold[0].bytes), mode="r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith(f"raw/{part}/window=100.bin") for n in names), names
    assert any(n.endswith(f"raw/{part}/window=200.bin") for n in names), names
    assert any(n.endswith(f"metadata/{part}/window=100.jsonl") for n in names), names

    hot = [u for u in up.uploads if u.bucket == "hot-bucket"]
    assert len(hot) == 1
    assert hot[0].bytes == b"PAR1fake"


def test_prune_raises_without_local_landing():
    """On the S3 path there is no local landing; prune must refuse rather than
    silently no-op (the S3 lifecycle rule handles S3 expiry instead)."""
    shipper = Shipper(
        source=S3Source(FakeUploader(), "landing-bucket", ""),
        curated_dir=Path("/tmp/none"),
        uploader=FakeUploader(),
        cold_bucket="c",
        hot_bucket="h",
    )
    with pytest.raises(RuntimeError, match="prune requires a local landing_dir"):
        shipper.prune()


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


# --- prune_s3 --------------------------------------------------------------- #
# The S3-landing counterpart to prune() above, used by the nightly batch's
# archive stage (pipeline/prune_s3.py).


@pytest.fixture
def s3_shipper():
    uploader = FakeUploader()
    return Shipper(
        source=S3Source(uploader, "landing-bucket", ""),
        curated_dir=Path("/tmp/none"),
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        feed_names=[FEED],
        feed_alerts_capable=[FEED],
        landing_bucket="landing-bucket",
        landing_prefix="",
    )


def _seed_s3_landing(uploader, feed, day):
    Y, M, D = day.year, day.month, day.day
    uploader.put(
        f"{feed}/metadata/year={Y}/month={M}/day={D}/data.jsonl", b'{"ts":1}\n'
    )
    uploader.put(f"{feed}/raw/year={Y}/month={M}/day={D}/1.bin", b"raw")


def _s3_raw_keys(uploader, feed, day):
    return uploader.list_keys(
        "landing-bucket",
        f"{feed}/raw/year={day.year}/month={day.month}/day={day.day}/",
    )


def test_prune_s3_requires_landing_bucket():
    shipper = Shipper(
        source=S3Source(FakeUploader(), "landing-bucket", ""),
        curated_dir=Path("/tmp/none"),
        uploader=FakeUploader(),
        cold_bucket="c",
        hot_bucket="h",
    )
    with pytest.raises(RuntimeError, match="S3 prune requires a staging bucket"):
        shipper.prune_s3()


def test_prune_s3_deletes_shipped_and_snapshotted_day(s3_shipper):
    _seed_s3_landing(s3_shipper.uploader, FEED, DAY)
    s3_shipper.uploader.mark_existing("cold-bucket", s3_shipper._cold_key(FEED, DAY))
    s3_shipper.uploader.mark_existing("hot-bucket", s3_shipper._snapshot_key(FEED, DAY))

    result = s3_shipper.prune_s3(keep_days=3)

    assert result == {"deleted": 1, "skipped": 0}
    assert not _s3_raw_keys(s3_shipper.uploader, FEED, DAY)


def test_prune_s3_skips_unshipped_day(s3_shipper):
    _seed_s3_landing(s3_shipper.uploader, FEED, DAY)
    # No cold tarball marked existing.

    result = s3_shipper.prune_s3(keep_days=3)

    assert result == {"deleted": 0, "skipped": 1}
    assert _s3_raw_keys(s3_shipper.uploader, FEED, DAY), (
        "deleted raw that wasn't shipped!"
    )


def test_prune_s3_skips_alerts_capable_feed_without_snapshot(s3_shipper):
    """Cold-ship runs before snapshot in the nightly chain (see
    agency_batch.py's run_agency docstring), so the cold tarball existing is
    not proof snapshot ever processed this day. A feed that OOMs in
    snapshot.py every night (see local.heavy_agencies in terraform/rollup.tf)
    must not have its landing data deleted just because it shipped cold."""
    _seed_s3_landing(s3_shipper.uploader, FEED, DAY)
    s3_shipper.uploader.mark_existing("cold-bucket", s3_shipper._cold_key(FEED, DAY))
    # No snapshot object seeded.

    result = s3_shipper.prune_s3(keep_days=3)

    assert result == {"deleted": 0, "skipped": 1}
    assert _s3_raw_keys(s3_shipper.uploader, FEED, DAY), "pruned before snapshot ran!"


def test_prune_s3_ignores_snapshot_for_non_alerts_feed():
    """A feed whose decoder never produces AlertRow gets no snapshot object,
    ever -- prune must not wait forever for one that will never exist."""
    uploader = FakeUploader()
    shipper = Shipper(
        source=S3Source(uploader, "landing-bucket", ""),
        curated_dir=Path("/tmp/none"),
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        feed_names=[FEED],
        feed_alerts_capable=[],  # not alerts-capable
        landing_bucket="landing-bucket",
        landing_prefix="",
    )
    _seed_s3_landing(uploader, FEED, DAY)
    uploader.mark_existing("cold-bucket", shipper._cold_key(FEED, DAY))

    result = shipper.prune_s3(keep_days=3)

    assert result == {"deleted": 1, "skipped": 0}


def test_prune_s3_dry_run_touches_nothing(s3_shipper):
    _seed_s3_landing(s3_shipper.uploader, FEED, DAY)
    s3_shipper.uploader.mark_existing("cold-bucket", s3_shipper._cold_key(FEED, DAY))
    s3_shipper.uploader.mark_existing("hot-bucket", s3_shipper._snapshot_key(FEED, DAY))

    result = s3_shipper.prune_s3(keep_days=3, dry_run=True)

    assert result == {"deleted": 1, "skipped": 0}  # would-be count
    assert _s3_raw_keys(s3_shipper.uploader, FEED, DAY), "dry-run deleted from s3"


def test_prune_s3_keeps_recent_days(s3_shipper):
    today = datetime.now(tz=timezone.utc).date()
    _seed_s3_landing(s3_shipper.uploader, FEED, today)
    s3_shipper.uploader.mark_existing("cold-bucket", s3_shipper._cold_key(FEED, today))
    s3_shipper.uploader.mark_existing(
        "hot-bucket", s3_shipper._snapshot_key(FEED, today)
    )

    result = s3_shipper.prune_s3(keep_days=3)

    assert result == {"deleted": 0, "skipped": 0}
    assert _s3_raw_keys(s3_shipper.uploader, FEED, today), (
        "pruned a day inside keep_days"
    )


def test_ship_one_skips_when_keys_exist(dirs):
    uploader = FakeUploader()
    shipper = Shipper(
        source=LocalSource(dirs / "landing"),
        curated_dir=dirs / "curated",
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
        feed_names=[FEED],
        landing_dir=dirs / "landing",
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
        source=LocalSource(dirs / "landing"),
        curated_dir=dirs / "curated",
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
        feed_names=[FEED],
        landing_dir=dirs / "landing",
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
        source=LocalSource(dirs / "landing"),
        curated_dir=dirs / "curated",
        uploader=uploader,
        cold_bucket="cold-bucket",
        hot_bucket="hot-bucket",
        cold_prefix="archive/",
        hot_prefix="curated/",
        feed_names=[FEED],
        landing_dir=dirs / "landing",
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


def _write_snapshot(dirs):
    snap_dir = (
        dirs
        / "curated"
        / "snapshots"
        / "alerts"
        / f"feed={FEED}"
        / f"year={DAY.year}"
        / f"month={DAY.month}"
        / f"day={DAY.day}"
    )
    snap_dir.mkdir(parents=True)
    (snap_dir / "data.json.gz").write_bytes(b"\x1f\x8bfake")


def test_ship_one_cold_only_skips_hot_and_snapshots(dirs, shipper):
    # Both curated outputs exist, so "no upload" can only mean cold_only skipped
    # them -- not that there was nothing to ship.
    _write_snapshot(dirs)

    shipper.ship_one(FEED, DAY, cold_only=True)
    uploader = shipper.uploader

    assert len([u for u in uploader.uploads if u.bucket == "cold-bucket"]) == 1
    assert [u for u in uploader.uploads if u.bucket == "hot-bucket"] == []


def test_run_threads_cold_only_through(dirs, shipper):
    _write_snapshot(dirs)

    shipper.run(cold_only=True)
    uploader = shipper.uploader

    assert len([u for u in uploader.uploads if u.bucket == "cold-bucket"]) == 1
    assert [u for u in uploader.uploads if u.bucket == "hot-bucket"] == []


def test_ship_one_cold_only_then_full_ship_skips_the_cold_reupload(dirs, shipper):
    """The archive-first + end-of-chain ship pair agency_batch runs.

    The second call must not re-upload the tarball to DEEP_ARCHIVE -- that's
    what makes putting the cold ship first essentially free.
    """
    _write_snapshot(dirs)

    shipper.ship_one(FEED, DAY, cold_only=True)
    shipper.ship_one(FEED, DAY)
    uploader = shipper.uploader

    assert len([u for u in uploader.uploads if u.bucket == "cold-bucket"]) == 1
    # parquet + snapshot, both from the second call
    assert len([u for u in uploader.uploads if u.bucket == "hot-bucket"]) == 2


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
        source=LocalSource(tmp_path),
        curated_dir=tmp_path,
        uploader=FakeUploader(),
        cold_bucket="c",
        hot_bucket="h",
        feed_names=[FEED],
        landing_dir=tmp_path,
    )
    assert list(shipper._discover()) == [(FEED, date(2020, 1, 1))]
