"""Tests for shard assignment (step 4: shard-across-processes).

The units under test live in `archiver/shard.py`: `belongs_to_shard(agency_id,
index, count)` answers yes/no for one (agency, shard) pair, and `shard_for(
agency_id, count)` returns the owning shard index. The properties below are what
make sharding *correct*: every agency lands on exactly one shard, all shards
agree across processes, and bad inputs are rejected loudly.
"""

import subprocess
import sys
import pytest
from archiver.shard import belongs_to_shard, shard_for
import os

# A small, representative set of agency_ids to assign. Real agency_ids live in
# config/feeds.yaml (the `agency_id:` field). Feel free to swap in the actual
# list, or load it from the config, once you decide how realistic you want the
# balance test to be.
AGENCY_IDS = [
    "KCM",
    "MTA_NYCT",
    "MARTA",
    "BAY_AREA_511",
    "WMATA",
    "SOUND_TRANSIT",
    "TRIMET",
    "SEPTA",
]


def assign(agency_ids, shard_count):
    """Helper: bucket agency_ids into {shard_index: [agency_id, ...]}.

    Built on shard_for, which answers "which shard?" directly, so we assign each
    agency in one pass rather than scanning every (agency, index) pair. Several
    tests below lean on this.
    """
    buckets = {i: [] for i in range(shard_count)}

    for agency_id in agency_ids:
        buckets[shard_for(agency_id, shard_count)].append(agency_id)
    return buckets


def _shard_in_subprocess(seed: str, agency_ids: list, shard_count: int):
    code = (
        "from archiver.shard import belongs_to_shard\n"
        f"agencies = {agency_ids!r}\n"  # !r => emits a valid list literal
        f"count = {shard_count}\n"
        "print(','.join(str(next(i for i in range(count) "
        "if belongs_to_shard(a, i, count))) for a in agencies))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
        ],  # sys.executable = the SAME python, not whatever's on PATH
        env={**os.environ, "PYTHONHASHSEED": seed},  # ← MERGE, don't replace
        capture_output=True,
        text=True,  # text=True => stdout is str, not bytes
        check=True,  # raise if the child crashed (import error, etc.)
    )
    return result.stdout.strip()


# --- Stability / determinism -------------------------------------------------
# The whole scheme breaks if two worker processes disagree about who owns an
# agency. Python's built-in hash() is salted per process (PYTHONHASHSEED), so a
# hash()-based impl would pass a same-process test yet fail in production.


def test_same_inputs_are_deterministic_within_a_process():
    """Calling belongs_to_shard twice with identical args returns the same bool.

    Weak on its own (a salted hash is stable *within* one process too) but it's
    the cheap floor. The real stability guarantee is the next test.
    """
    shard1 = belongs_to_shard("KCM", 0, 4)
    shard2 = belongs_to_shard("KCM", 0, 4)
    assert shard1 == shard2


def test_assignment_is_stable_across_processes():
    """A fresh interpreter must produce the SAME assignment as this one.

    Spawns two subprocesses with different PYTHONHASHSEED values and asserts they
    agree. A hash()-salted impl would disagree on some agency across seeds; a
    sha256-based one is identical. This is the most direct demonstration of the
    bug the scheme depends on not having.
    """
    a = _shard_in_subprocess("0", AGENCY_IDS, 4)  # full assignment over AGENCY_IDS
    b = _shard_in_subprocess("1", AGENCY_IDS, 4)  # different hostile seed
    assert a == b  # sha256 => equal; hash() => differ on some agency


# --- Coverage + disjointness -------------------------------------------------
# Across all shards, every agency is owned by exactly one shard: no gaps (a feed
# silently un-ingested) and no overlaps (two workers double-archiving + double
# rate-limit spend).


def test_every_agency_lands_on_exactly_one_shard():
    """For shard_count=N, each agency_id belongs to exactly one index in [0, N).

    Assert the per-agency count of "True" answers over all indices is exactly 1.
    """
    count = 4
    for agency_id in AGENCY_IDS:
        true_count = sum(belongs_to_shard(agency_id, i, count) for i in range(count))
        assert true_count == 1, f"{agency_id} belongs to {true_count} shards"


def test_union_of_all_shards_covers_every_agency():
    """Concatenating assign(...) buckets reproduces the full agency set.

    Same invariant as above viewed as a whole rather than per-agency — both are
    worth having; they fail differently and read differently.
    """
    buckets = assign(AGENCY_IDS, 4)
    flat = [a for bucket in buckets.values() for a in bucket]
    assert set(flat) == set(AGENCY_IDS)
    assert len(flat) == len(
        AGENCY_IDS
    )  # the set== alone hides duplicates; length catches them


# --- Single-shard identity ---------------------------------------------------


def test_count_one_selects_everything():
    """shard_count=1 => belongs_to_shard is True for every agency.

    This is the "no-flags / today's behavior" guarantee: one shard owns all
    feeds, so the default path is a behavioral no-op.
    """
    agencies = [f"agency-{i}" for i in range(1000)]
    count = 1
    sizes = [len(b) for b in assign(agencies, count).values()]
    assert sizes == [len(agencies)]  # all agencies in the one shard, none in others


# --- Balance sanity ----------------------------------------------------------


def test_shards_are_roughly_balanced():
    """No shard should be wildly lopsided.

    Asserts every shard is within 20% of the mean for 1000 synthetic agencies
    over 4 shards. This validates the spread of the hash function, not the
    balance of the real feed list (too few agencies to be meaningful). Because
    sha256 is deterministic, this passes or fails the same way every run rather
    than flaking.
    """
    agencies = [f"agency-{i}" for i in range(1000)]
    count = 4
    sizes = [len(b) for b in assign(agencies, count).values()]
    mean = len(agencies) / count  # 250
    for size in sizes:
        assert 0.8 * mean <= size <= 1.2 * mean  # within 20% of the mean


# --- Validation --------------------------------------------------------------
# belongs_to_shard rejects out-of-range and non-positive inputs loudly rather
# than silently mis-assigning.


def test_shard_index_must_be_less_than_count():
    """index == count (and index > count) is out of range and must raise."""
    with pytest.raises(ValueError):
        belongs_to_shard("KCM", 4, 4)
    with pytest.raises(ValueError):
        belongs_to_shard("KCM", 5, 4)


def test_negative_values_rejected():
    """Negative index or negative/zero count must raise."""
    with pytest.raises(ValueError):
        belongs_to_shard("KCM", -1, 4)
    with pytest.raises(ValueError):
        belongs_to_shard("KCM", 0, 0)
