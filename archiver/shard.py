import hashlib


def belongs_to_shard(agency_id: str, index: int, count: int) -> bool:
    """Return True if agency_id belongs to shard index in [0, count).

    The shard assignment is deterministic and stable across runs, and should
    balance agencies reasonably well across shards. The exact algorithm is not
    guaranteed and may change; the only guarantee is that for a given set of
    inputs, the same output will be produced.

    Args:
        agency_id: The ID of the agency to check.
        index: The index of the shard to check against, in the range [0, count).
        count: The total number of shards.  Must be a positive integer.
    """
    if count <= 0:
        raise ValueError(f"count {count} must be a positive integer")
    if not (0 <= index < count):
        raise ValueError(f"index {index} must be in the range [0, {count})")
    return shard_for(agency_id, count) == index


def shard_for(agency_id: str, count: int) -> int:
    # the one place the hash lives
    return int.from_bytes(hashlib.sha256(agency_id.encode()).digest(), "big") % count
