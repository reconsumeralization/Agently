# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing_extensions import Self

from agently.core import BaseAgent
from agently.core.storage import RecordStore
from agently.core.storage._defaults import default_record_store_root, script_scope


class RecordStoreExtension(BaseAgent):
    """Bind Agent persistence without coupling it to the task file boundary."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.record_store = self._create_record_store_binding()

    def _default_record_store_root(self) -> Path:
        configured = self.settings.get("record_store.default_root", None)
        if configured is not None:
            return Path(str(configured)).expanduser().resolve()
        return default_record_store_root()

    def _default_record_scope(self) -> dict[str, Any]:
        scope: dict[str, Any] = {"agent_id": self.id, "agent_name": self.name}
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            scope["session_id"] = str(session_id)
        else:
            scope["script_scope"] = script_scope(self.settings)
        project_id = self.settings.get("record_store.project_id", None)
        if project_id is not None:
            scope["project_id"] = str(project_id)
        return scope

    def _default_record_search_scope(self) -> dict[str, Any]:
        scope: dict[str, Any] = {}
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            scope["session_id"] = str(session_id)
        else:
            scope["script_scope"] = script_scope(self.settings)
        project_id = self.settings.get("record_store.project_id", None)
        if project_id is not None:
            scope["project_id"] = str(project_id)
        return scope

    def _create_record_store_binding(
        self,
        source: Any = None,
        *,
        mode: str = "read_only",
        create: bool = True,
    ) -> RecordStore:
        if isinstance(source, RecordStore):
            return source
        from agently.base import record_store_registry

        return record_store_registry.create(
            self._default_record_store_root() if source is None else source,
            mode=mode,
            create=create,
            default_scope=self._default_record_scope(),
            default_search_scope=self._default_record_search_scope(),
        )

    def _refresh_default_record_store_binding(self) -> None:
        store = getattr(self, "record_store", None)
        if store is not None and getattr(store, "_backend", None) is None:
            self.record_store = self._create_record_store_binding()
            bind_memory = getattr(self, "_bind_activated_session_memory_store", None)
            if callable(bind_memory):
                bind_memory()

    def use_record_store(
        self,
        source: Any,
        *,
        mode: str = "read_only",
        create: bool = True,
    ) -> Self:
        self.record_store = self._create_record_store_binding(
            source,
            mode=mode,
            create=create,
        )
        bind_memory = getattr(self, "_bind_activated_session_memory_store", None)
        if callable(bind_memory):
            bind_memory()
        return self


__all__ = ["RecordStoreExtension"]
