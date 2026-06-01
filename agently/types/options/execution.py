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
from typing import Any, Literal

from pydantic import ConfigDict

from agently.types.config import AgentlyConfigModel

from .routes import SkillsRouteOptions


class AgentExecutionRouteOptions(AgentlyConfigModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    skills: SkillsRouteOptions | None = None


class AgentExecutionLifecycleOptions(AgentlyConfigModel):
    mode: Literal["one_turn", "task_step"] | str | None = None
    lineage: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None


class ExecutionOptions(AgentlyConfigModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)
    __options_namespace__ = "execution"

    execution: AgentExecutionLifecycleOptions | None = None
    routes: AgentExecutionRouteOptions | None = None
    meta: dict[str, Any] | None = None


def normalize_execution_options(options: Any) -> dict[str, Any]:
    if options is None:
        return {}
    if isinstance(options, ExecutionOptions):
        return options.to_options_dict()
    if isinstance(options, AgentlyConfigModel):
        return ExecutionOptions.model_validate(options.to_options_dict()).to_options_dict()
    if isinstance(options, Mapping):
        return ExecutionOptions.model_validate(dict(options)).to_options_dict()
    raise TypeError(
        f"AgentExecution options must be a dict or ExecutionOptions, got { type(options).__name__ }."
    )
