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

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TYPE_CHECKING, cast

from agently.types.data import AgentExecutionPatternInfo
from agently.types.plugins import AgentPatternHandler, AgentPatternInput
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


_BUILTIN_PATTERNS = frozenset({"request", "goal"})
_PatternSource = Literal["builtin", "plugin", "instance", "handler"]
_DefaultRouteRunner = Callable[[], Awaitable[tuple[str, Any]]]


def default_pattern_info() -> AgentExecutionPatternInfo:
    return {
        "name": "request",
        "source": "builtin",
        "selected_by": "default",
        "status": "selected",
        "used_default": False,
    }


def declare_pattern(
    execution: "AgentExecution",
    pattern: AgentPatternInput,
) -> "AgentExecution":
    target = execution._reconfiguration_target()
    if isinstance(pattern, str):
        name = pattern.strip()
        if not name:
            raise ValueError("Agent pattern name must be a non-empty string.")
        source: _PatternSource = "builtin" if name in _BUILTIN_PATTERNS else "plugin"
        selection: Any = name
    else:
        run = getattr(pattern, "run", None)
        if callable(run):
            name = _pattern_name(pattern)
            source = "instance"
            selection = pattern
        elif callable(pattern):
            name = _pattern_name(pattern)
            source = "handler"
            selection = pattern
        else:
            raise TypeError(
                "Agent pattern must be a registered name, an AgentPattern instance, or a callable handler."
            )
    target.pattern_selection = selection
    target.pattern_info = cast(
        AgentExecutionPatternInfo,
        {
            "name": name,
            "source": source,
            "selected_by": "pattern",
            "status": "selected",
            "used_default": False,
        },
    )
    return target


def select_goal_pattern(execution: "AgentExecution") -> None:
    execution.pattern_selection = "goal"
    execution.pattern_info = cast(
        AgentExecutionPatternInfo,
        {
            "name": "goal",
            "source": "builtin",
            "selected_by": "goal",
            "status": "selected",
            "used_default": False,
        },
    )


async def run_selected_pattern(
    execution: "AgentExecution",
    run_default_route: _DefaultRouteRunner,
) -> tuple[str, Any]:
    execution._refresh_prompt_snapshot()
    selection = execution.pattern_selection
    if selection is None:
        return await _run_builtin_default(execution, "request", run_default_route, emit=False)
    if isinstance(selection, str) and selection == "request":
        return await _run_builtin_default(execution, "request", run_default_route, emit=True)
    if isinstance(selection, str) and selection == "goal":
        return await _run_builtin_default(execution, "goal", run_default_route, emit=True)

    try:
        name, source, runner = _resolve_pattern(execution, selection)
    except BaseException as error:
        execution.pattern_info["status"] = "failed"
        await _emit_pattern(
            execution,
            "pattern.failed",
            extra={
                "error_type": error.__class__.__name__,
                "message": (str(error).strip() or error.__class__.__name__)[:360],
            },
        )
        raise
    execution.pattern_info = cast(
        AgentExecutionPatternInfo,
        {
            "name": name,
            "source": source,
            "selected_by": "pattern",
            "status": "running",
            "used_default": False,
        },
    )
    await _emit_pattern(execution, "pattern.started")
    continuation = _RunDefaultOnce(run_default_route)
    try:
        value = runner(execution, continuation)
        result = await value if inspect.isawaitable(value) else value
        if continuation.duplicate_attempted:
            raise RuntimeError("AgentPattern run_default() may be called at most once.")
    except BaseException as error:
        execution.pattern_info["status"] = "failed"
        execution.pattern_info["used_default"] = continuation.called
        await _emit_pattern(
            execution,
            "pattern.failed",
            extra={
                "error_type": error.__class__.__name__,
                "message": (str(error).strip() or error.__class__.__name__)[:360],
            },
        )
        raise

    route = continuation.route or "agent_pattern"
    if continuation.route is None:
        route_meta = {"pattern": name, "selected_by": "agent_pattern"}
        execution.route_info = {
            "selected_route": route,
            "selected_by": "agent_pattern",
            "options": route_meta,
            "reusable": True,
        }
        execution.route_plan = execution.route_planner.build_route_plan(
            execution_id=execution.id,
            route=route,
            route_meta=route_meta,
        )
    execution.pattern_info["status"] = "completed"
    execution.pattern_info["used_default"] = continuation.called
    await _emit_pattern(execution, "pattern.completed", route=route)
    return route, result


async def _run_builtin_default(
    execution: "AgentExecution",
    name: Literal["request", "goal"],
    run_default_route: _DefaultRouteRunner,
    *,
    emit: bool,
) -> tuple[str, Any]:
    execution.pattern_info["name"] = name
    execution.pattern_info["source"] = "builtin"
    execution.pattern_info["status"] = "running"
    execution.pattern_info["used_default"] = True
    if emit:
        await _emit_pattern(execution, "pattern.started")
    try:
        route, result = await run_default_route()
    except BaseException as error:
        execution.pattern_info["status"] = "failed"
        if emit:
            await _emit_pattern(
                execution,
                "pattern.failed",
                extra={
                    "error_type": error.__class__.__name__,
                    "message": (str(error).strip() or error.__class__.__name__)[:360],
                },
            )
        raise
    execution.pattern_info["status"] = "completed"
    if emit:
        await _emit_pattern(execution, "pattern.completed", route=route)
    return route, result


def _resolve_pattern(
    execution: "AgentExecution",
    selection: Any,
) -> tuple[str, _PatternSource, AgentPatternHandler]:
    if isinstance(selection, str):
        try:
            plugin_class = execution.agent.plugin_manager.get_plugin("AgentPattern", selection)
        except (KeyError, TypeError) as error:
            raise ValueError(f"AgentPattern {selection!r} is not registered.") from error
        plugin = cast(Any, plugin_class)(
            plugin_manager=execution.agent.plugin_manager,
            settings=execution.agent.settings,
        )
        runner = getattr(plugin, "run", None)
        if not callable(runner):
            raise TypeError(f"Registered AgentPattern {selection!r} must define callable run(...).")
        return selection, "plugin", cast(AgentPatternHandler, runner)

    runner = getattr(selection, "run", None)
    if callable(runner):
        return _pattern_name(selection), "instance", cast(AgentPatternHandler, runner)
    if callable(selection):
        return _pattern_name(selection), "handler", cast(AgentPatternHandler, selection)
    raise TypeError(
        "Agent pattern must be a registered name, an AgentPattern instance, or a callable handler."
    )


class _RunDefaultOnce:
    def __init__(self, runner: _DefaultRouteRunner):
        self._runner = runner
        self.called = False
        self.duplicate_attempted = False
        self.route: str | None = None

    async def __call__(self) -> object:
        if self.called:
            self.duplicate_attempted = True
            raise RuntimeError("AgentPattern run_default() may be called at most once.")
        self.called = True
        self.route, result = await self._runner()
        return result


async def _emit_pattern(
    execution: "AgentExecution",
    path: str,
    *,
    route: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    value = {
        **DataFormatter.sanitize(execution.pattern_info),
        **DataFormatter.sanitize(extra or {}),
    }
    await execution.emit_stream(
        path,
        value,
        route=route,
        source="agent_pattern",
        meta={"pattern": execution.pattern_info["name"]},
    )


def _pattern_name(pattern: Any) -> str:
    return str(
        getattr(pattern, "name", None)
        or getattr(pattern, "__name__", None)
        or pattern.__class__.__name__
    ).strip()


__all__ = [
    "declare_pattern",
    "default_pattern_info",
    "run_selected_pattern",
    "select_goal_pattern",
]
