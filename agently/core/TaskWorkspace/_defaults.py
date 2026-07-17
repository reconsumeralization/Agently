from __future__ import annotations

import sys
from pathlib import Path


def default_task_workspace_root() -> Path:
    """Return the caller's entry-script directory without touching disk."""

    main_module = sys.modules.get("__main__")
    entry_file = getattr(main_module, "__file__", None)
    if isinstance(entry_file, str) and entry_file and not entry_file.startswith("<"):
        resolved_entry = Path(entry_file).expanduser().resolve()
        try:
            resolved_entry.relative_to(Path(sys.prefix).expanduser().resolve())
        except ValueError:
            return resolved_entry.parent
    return Path.cwd().resolve()


__all__ = ["default_task_workspace_root"]
