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
        include_gtfs=True,
    )

    assert result.ok
    scripts = [c[1] for c in calls]
    assert scripts == [
        "pipeline/rollup.py",
        "pipeline/rollup.py",
        "pipeline/gtfs.py",
        "pipeline/gold.py",
        "pipeline/ship.py",
        "pipeline/ship.py",
    ]

    # rollup/ship get one feed each, as separate invocations (singular --feed)
    rollup_feeds = {
        calls[0][calls[0].index("--feed") + 1],
        calls[1][calls[1].index("--feed") + 1],
    }
    assert rollup_feeds == {"wmata-trips", "wmata-vehicles"}

    # gtfs/gold get both feed names in one invocation, with --feed last (nargs="+" is greedy)
    gtfs_cmd = calls[2]
    assert gtfs_cmd[-3:] == ["--feed", "wmata-trips", "wmata-vehicles"]
    gold_cmd = calls[3]
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
        include_gtfs=False,
    )

    assert all("pipeline/gtfs.py" not in c for c in calls)


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
        include_gtfs=False,
    )

    assert not result.ok
    assert result.returncode == 1
    assert result.failed_cmd is not None and "pipeline/rollup.py" in result.failed_cmd
    # gold.py/ship.py must never have been invoked once the first rollup call failed
    assert all(
        "pipeline/gold.py" not in c and "pipeline/ship.py" not in c for c in calls
    )
    # only one rollup call happened -- the second feed's rollup never ran either
    assert sum(1 for c in calls if "pipeline/rollup.py" in c) == 1


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
        groups, config="cfg.yaml", day=DAY, force=False, include_gtfs=False, workers=2
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
        ["--config", str(config_path), "--day", DAY.isoformat(), "--workers", "1"]
    )
    assert exit_code == 1


def test_main_all_succeed_exit_code_zero(monkeypatch, config_path):
    monkeypatch.setattr(
        agency_batch.subprocess,
        "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0),
    )

    exit_code = agency_batch.main(
        ["--config", str(config_path), "--day", DAY.isoformat(), "--workers", "1"]
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
        ]
    )

    assert all("wmata" not in c for cmd in calls for c in cmd)
    assert any("nomdb-vehicles" in c for cmd in calls for c in cmd)
