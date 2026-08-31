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


def _built_feeds(monkeypatch, argv):
    """Run main() with build_one stubbed; return the feeds it tried to build."""
    seen: list[str] = []

    def fake_build_one(feed, day, *a, **kw):
        seen.append(feed)
        return None

    monkeypatch.setattr(gold, "build_one", fake_build_one)
    monkeypatch.setattr(gold, "_make_gtfs_resolver", lambda args: None)
    assert gold.main(argv) == 0
    return seen


def test_all_feeds_run_skips_superseded(config_path, listed, monkeypatch):
    seen = _built_feeds(
        monkeypatch, ["--config", str(config_path), "--day", "2026-08-31"]
    )
    assert "listed-vehicles" not in seen
    assert sorted(seen) == ["listed-trips", "unlisted-trips", "unlisted-vehicles"]


def test_explicit_feed_is_still_honored(config_path, listed, monkeypatch):
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
    )
    assert seen == ["listed-vehicles"]
