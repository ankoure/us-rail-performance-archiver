"""Which source gold builds an agency's marts from.

An agency on `_TRIP_UPDATES_ONLY_FEEDS` builds from its TripUpdates feed; its
other feeds must not also build, or the same (agency, day) lands twice under
two `feed=` partitions and dashboard/api — which reads every feed an agency
owns — double-counts them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import gold
from pipeline.gold import superseded_by_trip_updates

CONFIG_YAML = """
writer:
  landing_dir: ./landing
  curated_dir: ./curated
agencies:
  - agency_id: LISTED
    name: Built from trip updates
    region: Somewhere
    timezone: America/New_York
    base_url: https://example.com
    auth:
      type: none
    feeds:
      - name: listed-vehicles
        path: /vehicles
      - name: listed-trips
        path: /trips
      - name: listed-alerts
        path: /alerts
  - agency_id: UNLISTED
    name: Built from vehicles
    region: Elsewhere
    timezone: America/New_York
    base_url: https://example.com
    auth:
      type: none
    feeds:
      - name: unlisted-vehicles
        path: /vehicles
      - name: unlisted-trips
        path: /trips
"""


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "feeds.yaml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(gold, "_TRIP_UPDATES_ONLY_FEEDS", {"listed-trips"})


def test_supersedes_the_agencys_other_feeds(config_path, listed):
    assert superseded_by_trip_updates(config_path) == {
        "listed-vehicles",
        "listed-alerts",
    }


def test_leaves_unlisted_agencies_alone(config_path, listed):
    superseded = superseded_by_trip_updates(config_path)
    assert "unlisted-vehicles" not in superseded
    assert "unlisted-trips" not in superseded


def test_never_supersedes_the_listed_feed_itself(config_path, listed):
    assert "listed-trips" not in superseded_by_trip_updates(config_path)


def test_empty_when_no_agency_is_listed(config_path, monkeypatch):
    monkeypatch.setattr(gold, "_TRIP_UPDATES_ONLY_FEEDS", set())
    assert superseded_by_trip_updates(config_path) == set()


@pytest.fixture
def curated_dir(tmp_path) -> Path:
    """A curated root holding one silver partition.

    main() now refuses to run against a tree with no silver at all
    (gold.assert_silver_present), so these tests must supply one explicitly
    rather than inheriting the developer's local data/curated -- which is
    gitignored and absent in CI.
    """
    d = tmp_path / "curated"
    (
        d / "vehicles" / "feed=unlisted-vehicles" / "year=2026" / "month=8" / "day=31"
    ).mkdir(parents=True)
    return d


def _built_feeds(monkeypatch, argv, curated_dir):
    """Run main() with build_one stubbed; return the feeds it tried to build."""
    seen: list[str] = []

    def fake_build_one(feed, day, *a, **kw):
        seen.append(feed)
        return None

    monkeypatch.setattr(gold, "build_one", fake_build_one)
    monkeypatch.setattr(gold, "_make_gtfs_resolver", lambda args: None)
    assert gold.main([*argv, "--curated-dir", str(curated_dir)]) == 0
    return seen


def test_all_feeds_run_skips_superseded(config_path, listed, monkeypatch, curated_dir):
    seen = _built_feeds(
        monkeypatch, ["--config", str(config_path), "--day", "2026-08-31"], curated_dir
    )
    assert "listed-vehicles" not in seen
    assert sorted(seen) == ["listed-trips", "unlisted-trips", "unlisted-vehicles"]


def test_explicit_feed_is_still_honored(config_path, listed, monkeypatch, curated_dir):
    # An operator naming the feed outranks the default routing.
    seen = _built_feeds(
        monkeypatch,
        [
            "--config",
            str(config_path),
            "--feed",
            "listed-vehicles",
            "--day",
            "2026-08-31",
        ],
        curated_dir,
    )
    assert seen == ["listed-vehicles"]


def test_main_allows_a_sparse_tree(config_path, monkeypatch, curated_dir):
    """A feed with no partition of its own is a normal skip, not a failure --
    alerts-only feeds never have a vehicles/ partition."""
    monkeypatch.setattr(gold, "_make_gtfs_resolver", lambda args: None)
    assert (
        gold.main(
            ["--config", str(config_path), "--feed", "listed-trips"]
            + ["--day", "2026-08-31", "--curated-dir", str(curated_dir)]
        )
        == 0
    )


def test_empty_local_tree_is_not_fatal_without_silver_dir(
    config_path, monkeypatch, tmp_path
):
    """Regression: BENTON_AREA_TRANSPORTATION, 2026-09-03.

    agency_batch runs agencies concurrently and clean_agency_curated deletes
    each one's curated output as it ships, so the shared local tree is
    legitimately empty at arbitrary moments. An alerts-only agency writes no
    vehicles/ or trip_updates/ partition of its own, so the guard saw an empty
    tree and exited 1 -- failing the agency and skipping its ship. The guard is
    only valid for a silver tree gold did NOT produce, i.e. when --silver-dir
    is given.
    """
    empty = tmp_path / "empty-curated"
    empty.mkdir()
    monkeypatch.setattr(gold, "_make_gtfs_resolver", lambda args: None)
    assert (
        gold.main(
            ["--config", str(config_path), "--feed", "listed-alerts"]
            + ["--day", "2026-08-31", "--curated-dir", str(empty)]
        )
        == 0
    )


def test_empty_remote_tree_is_still_fatal_with_silver_dir(
    config_path, monkeypatch, tmp_path
):
    """The split gold task's case, which the guard exists for: gold pointed at
    someone else's silver tree that turns out to be empty."""
    empty = tmp_path / "empty-silver"
    empty.mkdir()
    monkeypatch.setattr(gold, "_make_gtfs_resolver", lambda args: None)
    monkeypatch.setattr(
        gold, "build_one", lambda *a, **kw: pytest.fail("must not build anything")
    )
    assert (
        gold.main(
            ["--config", str(config_path), "--day", "2026-08-31"]
            + ["--curated-dir", str(tmp_path / "out"), "--silver-dir", str(empty)]
        )
        == 1
    )
