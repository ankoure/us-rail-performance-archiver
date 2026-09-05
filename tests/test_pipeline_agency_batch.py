"""Tests for pipeline/agency_batch.py -- command construction and per-agency
failure isolation. subprocess.run is monkeypatched throughout; nothing here
spawns a real child process (~186 agencies x several steps would be far too
slow and non-deterministic for CI).
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from pipeline import agency_batch

CONFIG_YAML = """
writer:
  landing_dir: ./landing
  curated_dir: ./curated
agencies:
  - agency_id: WMATA
    name: WMATA
    region: DC
    timezone: America/New_York
    base_url: https://example.com
    mdb_feed_id: mdb-1847
    auth:
      type: none
    feeds:
      - name: wmata-vehicles
        path: /vehicles
      - name: wmata-trips
        path: /trips
  - agency_id: NOMDB
    name: No MDB Agency
    region: Nowhere
    timezone: America/New_York
    base_url: https://example.com
    auth:
      type: none
    feeds:
      - name: nomdb-vehicles
        path: /vehicles
"""

DAY = date(2026, 8, 17)


def _stages(*, gtfs: bool = False, snapshot: bool = False) -> list[str]:
    """The pre---stages default chain, so these tests keep asserting the
    behaviour the combined nightly task actually gets."""
    return agency_batch.resolve_stages(
        None, include_gtfs=gtfs, include_snapshot=snapshot
    )


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "feeds.yaml"
    p.write_text(CONFIG_YAML)
    return p


def test_agency_feed_groups(config_path):
    groups = agency_batch._agency_feed_groups(str(config_path))
    assert groups == {
        "WMATA": ["wmata-trips", "wmata-vehicles"],
        "NOMDB": ["nomdb-vehicles"],
    }


def test_run_agency_command_sequence(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    result = agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(gtfs=True),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert result.ok
    scripts = [c[1] for c in calls]
    assert scripts == [
        # archive-first: the cold tarball ships before anything can fail
        "pipeline/ship.py",
        "pipeline/ship.py",
        # each feed's rollup is immediately followed by ITS OWN hot-ship,
        # rather than deferring all shipping to the end of the chain -- see
        # run_agency's docstring for why (GO_AHEAD's silver never reaching S3
        # before gtfs OOMed downstream).
        "pipeline/rollup.py",
        "pipeline/ship.py",
        "pipeline/rollup.py",
        "pipeline/ship.py",
        "pipeline/gtfs.py",
        "pipeline/gold.py",
        # the catch-all final ship still runs for every feed, to pick up
        # gtfs/gold's output the interim ships above couldn't have shipped yet
        "pipeline/ship.py",
        "pipeline/ship.py",
    ]
    assert all("--cold-only" in c for c in calls[:2])
    assert all("--cold-only" not in c for c in calls[2:])

    # cold-ship, the interim per-feed ships, and the final ships each get one
    # feed per invocation (singular --feed)
    for pair in (calls[:2], (calls[2], calls[4]), (calls[3], calls[5]), calls[8:]):
        assert {c[c.index("--feed") + 1] for c in pair} == {
            "wmata-trips",
            "wmata-vehicles",
        }

    # gtfs/gold get both feed names in one invocation, with --feed last (nargs="+" is greedy)
    gtfs_cmd = calls[6]
    assert gtfs_cmd[-3:] == ["--feed", "wmata-trips", "wmata-vehicles"]
    gold_cmd = calls[7]
    assert gold_cmd[-3:] == ["--feed", "wmata-trips", "wmata-vehicles"]


def test_run_agency_without_gtfs_skips_it(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    agency_batch.run_agency(
        "NOMDB",
        ["nomdb-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert all("pipeline/gtfs.py" not in c for c in calls)


def test_run_agency_with_snapshot_included(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(snapshot=True),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    # snapshot runs once, with both feed names, --feed last (nargs="+" is greedy).
    # It sits after gold and before the final ship: nothing but the snapshot ship
    # reads its output, so a snapshot failure shouldn't cost the marts either.
    scripts = [c[1] for c in calls]
    assert scripts.count("pipeline/snapshot.py") == 1
    snapshot_at = scripts.index("pipeline/snapshot.py")
    assert scripts[snapshot_at - 1] == "pipeline/gold.py"
    assert scripts[snapshot_at + 1] == "pipeline/ship.py"
    assert calls[snapshot_at][-3:] == ["--feed", "wmata-trips", "wmata-vehicles"]


def test_run_agency_stops_after_first_failure(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        rc = 1 if "pipeline/rollup.py" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    result = agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert not result.ok
    assert result.returncode == 1
    assert result.failed_cmd is not None and "pipeline/rollup.py" in result.failed_cmd
    # gold.py and the final (full) ship must never have been invoked once the
    # first rollup call failed -- but the archive-first cold ships already have.
    assert all("pipeline/gold.py" not in c for c in calls)
    ships = [c for c in calls if "pipeline/ship.py" in c]
    assert len(ships) == 2 and all("--cold-only" in c for c in ships)
    # only one rollup call happened -- the second feed's rollup never ran either
    assert sum(1 for c in calls if "pipeline/rollup.py" in c) == 1


def test_run_agency_rollup_output_ships_even_when_gtfs_fails_after_it(monkeypatch):
    """Regression for the GO_AHEAD incident: before each feed's hot-ship moved
    to run immediately after ITS rollup, a downstream gtfs/gold OOM stopped
    the fail-fast chain before the single end-of-chain ship ever ran --
    silently discarding rollup's already-successful output along with it
    (see run_agency's docstring). gtfs failing must not undo rollup's ship."""
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        rc = 1 if "pipeline/gtfs.py" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    result = agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(gtfs=True),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert not result.ok
    assert result.failed_cmd is not None and "pipeline/gtfs.py" in result.failed_cmd
    # both feeds' rollup AND their interim (non-cold) hot-ship already ran,
    # before gtfs ever got a chance to fail
    assert sum(1 for c in calls if "pipeline/rollup.py" in c) == 2
    non_cold_ships = [
        c for c in calls if "pipeline/ship.py" in c and "--cold-only" not in c
    ]
    assert len(non_cold_ships) == 2
    assert {c[c.index("--feed") + 1] for c in non_cold_ships} == {
        "wmata-trips",
        "wmata-vehicles",
    }
    # gold never ran -- the chain still correctly stops at gtfs's failure
    assert all("pipeline/gold.py" not in c for c in calls)


def test_run_agency_interim_ship_failure_is_not_fatal(monkeypatch):
    """Same non-fatal treatment as the archive-first cold ship: a transient S3
    failure shipping rollup's freshly-produced silver must not also block
    gtfs/gold, since the catch-all ship at the end of the chain retries it.

    The interim and final ship.py invocations for a feed are identical
    commands, so this fails only the FIRST (interim) one per feed and lets
    the second (final, catch-all) succeed -- exactly what a real transient S3
    blip followed by a successful retry looks like.
    """
    calls = []
    ship_calls_per_feed: dict[str, int] = {}

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        if "pipeline/ship.py" in cmd and "--cold-only" not in cmd:
            feed = cmd[cmd.index("--feed") + 1]
            ship_calls_per_feed[feed] = ship_calls_per_feed.get(feed, 0) + 1
            if ship_calls_per_feed[feed] == 1:
                return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    result = agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert result.ok
    # gold still ran despite both feeds' interim ship failing
    assert sum(1 for c in calls if "pipeline/gold.py" in c) == 1
    # each feed's ship.py ran twice: the failed interim one, then the
    # successful final catch-all
    non_cold_ships = [
        c for c in calls if "pipeline/ship.py" in c and "--cold-only" not in c
    ]
    assert len(non_cold_ships) == 4


def test_run_agency_archive_first_ship_failure_is_not_fatal(monkeypatch):
    """A failed cold ship must not skip the agency's chain.

    The full ship at the end retries it, so treating a transient S3 failure here
    as fatal would trade one missing tarball for the whole agency's output.
    """
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        rc = 1 if "--cold-only" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    result = agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert result.ok
    scripts = [c[1] for c in calls]
    assert scripts == [
        "pipeline/ship.py",
        "pipeline/ship.py",
        "pipeline/rollup.py",
        "pipeline/ship.py",
        "pipeline/rollup.py",
        "pipeline/ship.py",
        "pipeline/gold.py",
        "pipeline/ship.py",
        "pipeline/ship.py",
    ]


def test_resolve_stages_defaults_match_the_include_flags():
    assert agency_batch.resolve_stages(
        None, include_gtfs=False, include_snapshot=False
    ) == ["cold-ship", "rollup", "gold", "hot-ship"]
    assert agency_batch.resolve_stages(
        None, include_gtfs=True, include_snapshot=True
    ) == ["cold-ship", "rollup", "gtfs", "gold", "snapshot", "hot-ship"]


def test_resolve_stages_is_canonically_ordered_not_argv_ordered():
    assert agency_batch.resolve_stages(
        ["hot-ship", "gold", "rollup"], include_gtfs=False, include_snapshot=False
    ) == ["rollup", "gold", "hot-ship"]


def test_resolve_stages_implies_hot_ship_for_producers():
    """Selecting a producer without hot-ship would strand parquet on ephemeral
    disk -- the silent no-op that sank the earlier stage-split attempt."""
    for producer in ("rollup", "gtfs", "gold", "snapshot"):
        got = agency_batch.resolve_stages(
            [producer], include_gtfs=False, include_snapshot=False
        )
        assert got == [producer, "hot-ship"], producer


def test_resolve_stages_cold_ship_alone_does_not_imply_hot_ship():
    """cold-ship produces nothing in curated_dir, so the archive-only task
    shouldn't pay for a pointless hot ship of every feed."""
    assert agency_batch.resolve_stages(
        ["cold-ship"], include_gtfs=False, include_snapshot=False
    ) == ["cold-ship"]


def test_run_agency_single_stage_runs_only_that_stage(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=agency_batch.resolve_stages(
            ["snapshot"], include_gtfs=False, include_snapshot=False
        ),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    # snapshot (one call, all feeds) then the implied hot ship (one per feed).
    # No cold ship, no rollup, no gold.
    assert [c[1] for c in calls] == [
        "pipeline/snapshot.py",
        "pipeline/ship.py",
        "pipeline/ship.py",
    ]
    assert all("--cold-only" not in c for c in calls)


def test_run_agency_cold_ship_stage_alone(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=agency_batch.resolve_stages(
            ["cold-ship"], include_gtfs=False, include_snapshot=False
        ),
        curated_dir=Path("unused"),
        cleanup=False,
    )

    assert [c[1] for c in calls] == ["pipeline/ship.py", "pipeline/ship.py"]
    assert all("--cold-only" in c for c in calls)


def test_run_all_one_agency_failing_does_not_block_others(monkeypatch):
    def fake_run(cmd, *a, **kw):
        rc = 1 if "WMATA" in " ".join(cmd) or any("wmata" in c for c in cmd) else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    groups = {
        "WMATA": ["wmata-trips", "wmata-vehicles"],
        "NOMDB": ["nomdb-vehicles"],
    }
    results = agency_batch.run_all(
        groups,
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        workers=2,
        curated_dir=Path("unused"),
        cleanup=False,
    )

    by_agency = {r.agency_id: r for r in results}
    assert not by_agency["WMATA"].ok
    assert by_agency["NOMDB"].ok


def test_main_exit_code_reflects_failures(monkeypatch, config_path):
    def fake_run(cmd, *a, **kw):
        rc = 1 if any("wmata" in c for c in cmd) else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    exit_code = agency_batch.main(
        [
            "--config",
            str(config_path),
            "--day",
            DAY.isoformat(),
            "--workers",
            "1",
            "--no-cleanup",
        ]
    )
    assert exit_code == 1


def test_main_all_succeed_exit_code_zero(monkeypatch, config_path):
    monkeypatch.setattr(
        agency_batch.subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0),
    )

    exit_code = agency_batch.main(
        [
            "--config",
            str(config_path),
            "--day",
            DAY.isoformat(),
            "--workers",
            "1",
            "--no-cleanup",
        ]
    )
    assert exit_code == 0


def test_main_agency_filter_narrows_run(monkeypatch, config_path):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(agency_batch.subprocess, "run", fake_run)

    agency_batch.main(
        [
            "--config",
            str(config_path),
            "--day",
            DAY.isoformat(),
            "--agency",
            "NOMDB",
            "--workers",
            "1",
            "--no-cleanup",
        ]
    )

    assert all("wmata" not in c for cmd in calls for c in cmd)
    assert any("nomdb-vehicles" in c for cmd in calls for c in cmd)


def test_clean_agency_curated_deletes_only_named_feeds(tmp_path):
    curated = tmp_path / "curated"
    for rel in [
        "vehicles/feed=wmata-vehicles/year=2026/month=8/day=17/data.parquet",
        "trip_updates/feed=wmata-trips/year=2026/month=8/day=17/data.parquet",
        "metrics/stop_day/feed=wmata-vehicles/year=2026/month=8/day=17/data.parquet",
        "metrics/gtfs_stops/feed=wmata-vehicles/version=v1/data.parquet",
        "snapshots/alerts/feed=wmata-alerts/year=2026/month=8/day=17/data.json.gz",
        # a different agency's data, must survive untouched
        "vehicles/feed=metrostl-vehicles/year=2026/month=8/day=17/data.parquet",
    ]:
        path = curated / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")

    agency_batch.clean_agency_curated(
        curated, ["wmata-vehicles", "wmata-trips", "wmata-alerts"]
    )

    remaining = sorted(
        p.relative_to(curated).as_posix() for p in curated.rglob("*") if p.is_file()
    )
    assert remaining == [
        "vehicles/feed=metrostl-vehicles/year=2026/month=8/day=17/data.parquet"
    ]


def test_clean_agency_curated_missing_feed_is_a_noop(tmp_path):
    curated = tmp_path / "curated"
    curated.mkdir()
    # should not raise even though nothing exists for this feed yet
    agency_batch.clean_agency_curated(curated, ["never-existed"])


def test_run_agency_cleans_up_on_success_and_on_failure(monkeypatch, tmp_path):
    curated = tmp_path / "curated"
    for feed in ["wmata-trips", "wmata-vehicles"]:
        path = curated / f"vehicles/feed={feed}/year=2026/month=8/day=17/data.parquet"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")

    monkeypatch.setattr(
        agency_batch.subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0),
    )
    agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=curated,
        cleanup=True,
    )
    assert not any(curated.rglob("feed=wmata-*"))

    # recreate, then verify cleanup also runs when a step fails partway through
    for feed in ["wmata-trips", "wmata-vehicles"]:
        path = curated / f"vehicles/feed={feed}/year=2026/month=8/day=17/data.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    monkeypatch.setattr(
        agency_batch.subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 1),
    )
    agency_batch.run_agency(
        "WMATA",
        ["wmata-trips", "wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=curated,
        cleanup=True,
    )
    assert not any(curated.rglob("feed=wmata-*"))


def test_run_agency_no_cleanup_leaves_files(monkeypatch, tmp_path):
    curated = tmp_path / "curated"
    path = (
        curated / "vehicles/feed=wmata-vehicles/year=2026/month=8/day=17/data.parquet"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")

    monkeypatch.setattr(
        agency_batch.subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0),
    )
    agency_batch.run_agency(
        "WMATA",
        ["wmata-vehicles"],
        config="cfg.yaml",
        day=DAY,
        force=False,
        stages=_stages(),
        curated_dir=curated,
        cleanup=False,
    )
    assert path.exists()
