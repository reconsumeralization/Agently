# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_canonical(item) for item in value]
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {
        "type": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
        "repr": repr(value),
    }


def _scope_digest(scope: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _canonical(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_source_cursor(
    *,
    source_id: str,
    source_revision: str,
    scope: Mapping[str, Any],
    offset: int,
) -> str:
    if offset < 0:
        raise ValueError("Context source cursor offset cannot be negative.")
    payload = {
        "version": 1,
        "source_id": source_id,
        "source_revision": source_revision,
        "scope_digest": _scope_digest(scope),
        "offset": offset,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_source_cursor(
    cursor: str | None,
    *,
    source_id: str,
    source_revision: str,
    scope: Mapping[str, Any],
) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 4096:
        raise ValueError("Context source cursor is invalid.")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Context source cursor cannot be decoded.") from error
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise ValueError("Context source cursor version is invalid.")
    if payload.get("source_id") != source_id:
        raise ValueError("Context source cursor belongs to a different source.")
    if payload.get("source_revision") != source_revision:
        raise ValueError("Context source cursor belongs to a stale source revision.")
    if payload.get("scope_digest") != _scope_digest(scope):
        raise ValueError("Context source cursor belongs to a different scope.")
    offset = payload.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Context source cursor offset is invalid.")
    return offset


__all__ = ["decode_source_cursor", "encode_source_cursor"]
