"""Single source of truth for landing-zone window-object identity.

Both the continuous `LandingUploader` (poller, s3 mode) and the one-shot
`LandingBackfill` (soak-phase parity) ship the *same* local window objects to
the *same* S3 keys. If they computed selection or keys differently, a backfill
"parity check" would be comparing two different layouts — the verifier would be
the source of the discrepancy. So selection (`WINDOW_OBJECT_GLOBS`,
`iter_window_objects`) and key mapping (`window_object_key`) live here, once.

Layout mirrors `BatchingWriter._window_key` (writer.py:235-241):
    {feed}/{kind}/year=YYYY/month=MM/day=DD/window=*.{ext}
The daily `data.jsonl` (writer.py `append_metadata`) sits in the same metadata
directory but is local-only — it is excluded for free because its name doesn't
match `window=*`. Each glob `*` matches exactly one path segment (pathlib never
crosses "/"), so the patterns are pinned to that depth on purpose; a layout
change in writer.py must be reflected here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

WINDOW_OBJECT_GLOBS: tuple[str, ...] = (
    "*/raw/year=*/month=*/day=*/window=*.bin",
    "*/metadata/year=*/month=*/day=*/window=*.jsonl",
)


def iter_window_objects(landing_dir: Path) -> Iterator[Path]:
    """Yield every shippable window object under ``landing_dir`` (unordered).

    Files only — a directory whose name happens to match the glob is skipped,
    and in-progress ``*.tmp`` writes never match the pinned suffix.
    """
    for pattern in WINDOW_OBJECT_GLOBS:
        for p in landing_dir.glob(pattern):
            if p.is_file():
                yield p


def window_object_key(landing_dir: Path, path: Path, prefix: str = "") -> str:
    """Map a local window-object path to its S3 key.

    ``prefix + <path relative to landing_dir>``, slash-joined — byte-identical
    to the old synchronous ``S3Sink`` keys so the rollup reads the same layout.
    Caller is responsible for ``prefix`` ending in "/" (validated where used).
    """
    return prefix + path.relative_to(landing_dir).as_posix()


def hour_s3_key(prefix: str, feed: str, hour_unix: int, kind: str, ext: str) -> str:
    """S3 key for a merged hourly object.

    Mirrors the window key layout but replaces ``window={unix}`` with
    ``hour={unix}`` so the rollup's ``list_keys`` prefix scan returns both
    window and hour objects transparently.  ``kind`` is ``"raw"`` for ``.bin``
    and ``"metadata"`` for ``.jsonl``.
    """
    dt = datetime.fromtimestamp(hour_unix, tz=timezone.utc)
    rel = (
        f"{feed}/{kind}/year={dt.year}/month={dt.month}"
        f"/day={dt.day}/hour={hour_unix}.{ext}"
    )
    return f"{prefix}{rel}"
