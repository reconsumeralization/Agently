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

"""Builtin Skills effort strategy handlers.

These handlers are intentionally split from the executor so application
developers can read them as reference implementations for custom
`SkillsEffortStrategyHandler` functions.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from .single_shot import run_single_shot_strategy
from .runtime_chain import run_runtime_chain_strategy
from .staged import run_staged_strategy
from .react import run_react_strategy


BUILTIN_EFFORT_STRATEGY_NAMES = ("single_shot", "runtime_chain", "staged", "react")


def create_builtin_effort_strategy_handlers(executor: Any) -> dict[str, Any]:
    return {
        "single_shot": partial(run_single_shot_strategy, executor=executor),
        "runtime_chain": partial(run_runtime_chain_strategy, executor=executor),
        "staged": partial(run_staged_strategy, executor=executor),
        "react": partial(run_react_strategy, executor=executor),
    }


__all__ = [
    "BUILTIN_EFFORT_STRATEGY_NAMES",
    "create_builtin_effort_strategy_handlers",
    "run_single_shot_strategy",
    "run_runtime_chain_strategy",
    "run_staged_strategy",
    "run_react_strategy",
]
