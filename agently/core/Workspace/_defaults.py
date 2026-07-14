# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._utils import slug


def default_workspace_root() -> Path:
    """Return the entry script directory, or cwd, without touching disk."""

    main_module = sys.modules.get("__main__")
    entry_file = getattr(main_module, "__file__", None)
    if isinstance(entry_file, str) and entry_file and not entry_file.startswith("<"):
        resolved_entry = Path(entry_file).expanduser().resolve()
        try:
            resolved_entry.relative_to(Path(sys.prefix).expanduser().resolve())
        except ValueError:
            return resolved_entry.parent
    return Path.cwd().resolve()


def script_scope(settings: Any = None) -> str:
    """Return a logical record label; it never changes the file root."""

    configured = _settings_get(settings, "workspace.script_scope")
    if configured is not None:
        return slug(str(configured), "script")
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and Path(argv0).stem:
        return slug(Path(argv0).stem, "script")
    return slug(Path.cwd().name, "script")


def merge_scope(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    merged = {key: value for key, value in dict(base or {}).items() if value is not None}
    merged.update({key: value for key, value in dict(override or {}).items() if value is not None})
    return merged


def _settings_get(settings: Any, key: str) -> Any:
    if settings is None:
        return None
    getter = getattr(settings, "get", None)
    if callable(getter):
        return getter(key, None)
    if isinstance(settings, dict):
        return settings.get(key)
    return None


__all__ = ["default_workspace_root", "merge_scope", "script_scope"]
