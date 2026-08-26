import hashlib


def belongs_to_shard(
    agency_id: str, index: int, count: int, pin: int | None = None
) -> bool:
    """Return True if agency_id belongs to shard index.

    The shard assignment is deterministic and stable across runs, and should
    balance agencies reasonably well across shards. The exact algorithm is not
    guaranteed and may change; the only guarantee is that for a given set of
    inputs, the same output will be produced.

    Args:
        agency_id: The ID of the agency to check.
        index: shard index being checked against. Must be a non-negative
            integer. For unpinned agencies, any index >= count simply
            can't match (returns False) -- it's not an error, since a
            dedicated/pinned shard's index commonly sits outside the
            general pool's range and callers will legitimately ask "does
            this ordinary agency belong to shard <pinned index>?".
        count: size of the general (unpinned) pool. Must be a positive
            integer.
        pin: this agency's configured shard index, or None if it hashes
            into the general pool like today. Must be a non-negative
            integer if given. It may overlap with the general pool's
            range or lie entirely outside it -- both are valid, and it's
            not this function's concern whether some other agency's pin
            or hash also lands on the same index.
    """
    if count <= 0:
        raise ValueError(f"count {count} must be a positive integer")
    if index < 0:
        raise ValueError(f"index {index} must be a non-negative integer")

    if pin is not None:
        if pin < 0:
            raise ValueError(f"pin {pin} must be a non-negative integer")
        return index == pin

    if index >= count:
        return False
    return shard_for(agency_id, count) == index


def shard_for(agency_id: str, count: int) -> int:
    # the one place the hash lives
    return int.from_bytes(hashlib.sha256(agency_id.encode()).digest(), "big") % count
