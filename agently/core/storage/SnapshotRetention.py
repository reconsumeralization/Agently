from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agently.types.data.record_store import SnapshotRetentionPolicy


def normalize_snapshot_retention(
    policy: Mapping[str, Any] | None,
    *,
    default_keep_last: int | None,
) -> SnapshotRetentionPolicy:
    if policy is None:
        return {"keep_last": default_keep_last}
    unknown = sorted(set(policy) - {"keep_last"})
    if unknown:
        raise ValueError("Snapshot retention policy contains unsupported keys: " f"{', '.join(unknown)}.")
    keep_last = policy.get("keep_last", default_keep_last)
    if keep_last is not None:
        if isinstance(keep_last, bool) or not isinstance(keep_last, int):
            raise TypeError("snapshot retention keep_last must be a positive integer or None.")
        if keep_last < 1:
            raise ValueError("snapshot retention keep_last must be greater than or equal to 1.")
    return {"keep_last": keep_last}


__all__ = ["normalize_snapshot_retention"]
