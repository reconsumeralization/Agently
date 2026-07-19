# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .base import AgentlyPlugin


@runtime_checkable
class CodeRuntimeAdapter(AgentlyPlugin, Protocol):
    language_id: str
    aliases: tuple[str, ...]

    def prepare(
        self,
        request: "CodeExecutionRequest",
        policy: Mapping[str, Any],
    ) -> "CodeExecutionBundle": ...

    def toolchain_requirements(
        self,
    ) -> tuple["CodeExecutionToolchainRequirement", ...]: ...


from agently.types.data import (  # noqa: E402
    CodeExecutionBundle,
    CodeExecutionRequest,
    CodeExecutionToolchainRequirement,
)


__all__ = ["CodeRuntimeAdapter"]
