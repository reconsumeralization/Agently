"""Typed handoff from the shared execution lifecycle to its producer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agently.types.data import OutputValidateHandler


@dataclass(frozen=True)
class ProductionOptions:
    """Final request-reader options; internal stage schemas remain producer-owned."""

    type: Literal["original", "parsed", "all"] = "parsed"
    ensure_keys: list[str] | None = None
    ensure_all_keys: bool | None = None
    validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None
    key_style: Literal["dot", "slash"] = "dot"
    max_retries: int = 3
    raise_ensure_failure: bool = True
