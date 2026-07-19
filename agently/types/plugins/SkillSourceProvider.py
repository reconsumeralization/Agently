# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .base import AgentlyPlugin

if TYPE_CHECKING:
    from agently.types.data import SkillSourceRequest, SkillSourceSnapshot


@runtime_checkable
class SkillSourceProvider(AgentlyPlugin, Protocol):
    provider_id: str
    source_types: tuple[str, ...]

    async def async_materialize(
        self,
        request: "SkillSourceRequest",
    ) -> "SkillSourceSnapshot": ...


__all__ = ["SkillSourceProvider"]
