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

"""Canonical internal Skills manager plugin protocol.

``SkillsManager`` is the framework-facing owner for Skill installation,
discovery, activation, context packaging, capability need discovery, and
policy-gated action candidate brokering. It intentionally does not execute
Skill resources, grant permissions, own sandbox lifecycles, or accept task
completion.

``SkillsExecutor`` remains as the legacy public compatibility protocol. The two
protocols are shape-compatible during the migration so older plugins and
callers continue to work while internal code depends on ``SkillsManager``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .SkillsExecutor import (
    SkillsEffortStrategyHandler,
    SkillsExecutionContext,
    SkillsExecutor,
    SkillsPlanningContext,
    SkillsRuntimeContext,
)


@runtime_checkable
class SkillsManager(SkillsExecutor, Protocol):
    """Internal canonical Skills capability manager protocol."""


__all__ = [
    "SkillsEffortStrategyHandler",
    "SkillsExecutionContext",
    "SkillsManager",
    "SkillsPlanningContext",
    "SkillsRuntimeContext",
]
