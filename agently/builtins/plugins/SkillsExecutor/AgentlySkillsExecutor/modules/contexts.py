# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

from typing import Any

from agently.types.plugins import SkillsExecutionContext
from agently.utils.DataGuardian import _copy_public


class RuntimeStreamCaptureContext:
    def __init__(self, context: SkillsExecutionContext, runtime_stream: list[dict[str, Any]]):
        self._context = context
        self._runtime_stream = runtime_stream

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None:
        self._runtime_stream.append(_copy_public(item))
        await self._context.async_emit_runtime_stream(item)
