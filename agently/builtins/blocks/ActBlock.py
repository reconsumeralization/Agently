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

"""ActBlock — instrumented tool/action/script execution on the acting surface."""

from __future__ import annotations

import uuid
from typing import Any

from agently.builtins.blocks._protocol import FlowBlock


class ActBlock(FlowBlock):
    """Acting surface block wrapping tool/action/script execution.

    Gating is default-deny: the block checks the declared affordances and
    trust level before executing. Script execution routes through
    ``execution_environment``, never raw ``subprocess``.
    """

    name = "ActBlock"

    def __init__(
        self,
        *,
        allowed_tools: set[str] | None = None,
        allowed_actions: set[str] | None = None,
        allow_scripts: bool = False,
        artifact_inline_limit: int = 4096,
        default_deny: bool = True,
    ):
        self._allowed_tools = allowed_tools or set()
        self._allowed_actions = allowed_actions or set()
        self._allow_scripts = allow_scripts
        self._artifact_inline_limit = artifact_inline_limit
        self._default_deny = default_deny

    # ── Direct execution ──

    async def execute(
        self,
        *,
        action_spec: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        """Execute a single action.

        *action_spec* shape::

            {
                "type": "tool" | "action" | "script",
                "name": str,
                "kwargs": dict,
            }
        """
        act_type = action_spec.get("type", "tool")
        act_name = action_spec.get("name", "")
        act_kwargs = action_spec.get("kwargs", {}) or {}

        # Gate: authorize the action
        await self._authorize(act_type, act_name, context)

        await context.async_emit_runtime_stream(
            {
                "type": "block.act",
                "action": "start",
                "payload": {"act_type": act_type, "name": act_name},
            }
        )

        result: Any = None
        error: str | None = None

        try:
            if act_type == "script":
                result = await self._execute_script(act_name, act_kwargs, context)
            elif act_type == "action":
                result = await context.async_call_action(act_name, **act_kwargs)
            else:
                result = await context.async_call_tool(act_name, **act_kwargs)
        except Exception as exc:
            error = str(exc)
            result = {"error": error}

        await context.async_emit_runtime_stream(
            {
                "type": "block.act",
                "action": "done",
                "payload": {
                    "act_type": act_type,
                    "name": act_name,
                    "error": error,
                    "result_size": len(str(result)),
                },
            }
        )

        return {
            "act_type": act_type,
            "name": act_name,
            "result": result,
            "error": error,
        }

    async def _authorize(
        self, act_type: str, act_name: str, context: Any
    ) -> None:
        if not self._default_deny:
            return

        if act_type == "tool" and act_name not in self._allowed_tools:
            raise PermissionError(
                f"Tool '{act_name}' is not in allowed_tools for this execution."
            )

        if act_type == "action" and act_name not in self._allowed_actions:
            raise PermissionError(
                f"Action '{act_name}' is not in allowed_actions for this execution."
            )

        if act_type == "script" and not self._allow_scripts:
            raise PermissionError(
                "Script execution is not allowed for this execution."
            )

        ee = context.execution_environment
        if act_type == "script" and ee is None:
            raise RuntimeError(
                "Script execution requires an ExecutionEnvironment, but none is available."
            )

    async def _execute_script(
        self, script_name: str, kwargs: dict[str, Any], context: Any
    ) -> Any:
        ee = context.execution_environment
        return await ee.async_run_script(script_name, **kwargs)

    # ── TriggerFlow operator builder ──

    def build_operators(
        self,
        *,
        blueprint: Any,
        context: Any,
        settings: dict[str, Any] | None = None,
    ) -> list[str]:
        cfg = settings or {}
        allowed_tools = set(cfg.get("allowed_tools", []))
        allowed_actions = set(cfg.get("allowed_actions", []))
        allow_scripts = cfg.get("allow_scripts", self._allow_scripts)
        artifact_inline_limit = cfg.get(
            "artifact_inline_limit", self._artifact_inline_limit
        )

        block_id = f"act-block-{uuid.uuid4().hex[:8]}"
        event_start = f"{block_id}.start"
        event_done = f"{block_id}.done"

        # Build a configured instance for this operator
        configured = ActBlock(
            allowed_tools=allowed_tools,
            allowed_actions=allowed_actions,
            allow_scripts=allow_scripts,
            artifact_inline_limit=artifact_inline_limit,
            default_deny=self._default_deny,
        )

        async def handler(data: Any) -> Any:
            action_spec = data.value if isinstance(data.value, dict) else {}
            return await configured.execute(
                action_spec=action_spec,
                context=context,
            )

        blueprint.add_handler("event", event_start, handler, id=block_id)
        blueprint.definition.add_operator(
            id=block_id,
            kind="chunk",
            name=self.name,
            listen_signals=[
                {
                    "id": f"{block_id}.listen",
                    "trigger_type": "event",
                    "trigger_event": event_start,
                }
            ],
            emit_signals=[
                {
                    "id": f"{block_id}.emit",
                    "trigger_type": "event",
                    "trigger_event": event_done,
                }
            ],
            handler_ref={"source": "block", "block": self.name},
            options={
                "allowed_tools": list(allowed_tools),
                "allowed_actions": list(allowed_actions),
                "allow_scripts": allow_scripts,
                "artifact_inline_limit": artifact_inline_limit,
            },
        )

        return [block_id]
