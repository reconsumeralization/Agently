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

# ── Agent → Plugin context adapter ────────────────────────────────────────────
# AgentSkillsRuntimeContext bridges the Agent's internal API (settings, action
# registry, model requests, action dispatch) to the SkillsExecutor plugin
# protocols (SkillsPlanningContext / SkillsExecutionContext / SkillsRuntimeContext).
#
# This adapter pattern keeps the plugin implementation decoupled from concrete
# Agent internals. When other plugins need Agent-owned services, follow this
# pattern: define a context protocol in types/plugins/, implement the adapter
# here or in a sibling module, and provide a factory function.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

from pathlib import Path
from typing import Any

from agently.types.data import ActionResult, SkillExecutionPlan
from agently.types.plugins import SkillsRuntimeContext


_DEFAULT_BASH_ACTION_ALIASES = {"bash", "shell", "sh", "cmd", "run_bash", "bash_sandbox"}


def _ensure_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


class AgentSkillsRuntimeContext:
    """Agent component adapter passed into SkillsExecutor plugins."""

    def __init__(self, agent: Any):
        self.agent = agent

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.agent.settings.get(key, default)

    def action_available(self, action_id: str) -> bool:
        action = getattr(self.agent, "action", None)
        registry = getattr(action, "action_registry", None)
        if registry is not None and registry.has(action_id):
            return True
        from agently.base import action_registry

        return bool(action_registry.has(action_id))

    def can_auto_bind_bash_action(self, action_id: str) -> bool:
        if self.get_setting("skills.action_resolution.auto_enable_bash", True) is False:
            return False
        configured_aliases = _ensure_string_list(self.get_setting("skills.action_resolution.bash_action_aliases"))
        aliases = {item.strip().lower() for item in configured_aliases if item.strip()} or _DEFAULT_BASH_ACTION_ALIASES
        return action_id.strip().lower() in aliases

    def auto_bind_bash_action(self, action_id: str) -> None:
        action = getattr(self.agent, "action", None)
        register = getattr(action, "register_bash_sandbox_action", None)
        if not callable(register):
            raise RuntimeError("agent.action.register_bash_sandbox_action is not available.")
        allowed_prefixes_setting = self.get_setting("skills.action_resolution.bash_allowed_cmd_prefixes", None)
        allowed_prefixes = None
        if allowed_prefixes_setting is not None:
            allowed_prefixes = _ensure_string_list(allowed_prefixes_setting)
        timeout = int(self.get_setting("skills.action_resolution.bash_timeout", 20) or 20)
        register(
            action_id=action_id,
            desc=(
                "Auto-bound by Skills Executor as a controlled Bash substitute for a Skill action. "
                "Runs allowlisted shell commands inside the current workspace boundary."
            ),
            expose_to_model=False,
            allowed_cmd_prefixes=allowed_prefixes,
            allowed_workdir_roots=[str(Path.cwd().resolve())],
            timeout=timeout,
        )

    async def async_request_model_plan(
        self,
        *,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
        output_schema: dict[str, Any],
        max_revisions: int,
    ) -> dict[str, Any]:
        cards = [
            dict(item).get("card", {})
            for item in (plan.get("selected_skills") or [])
            if isinstance(item, dict)
        ]
        request = (
            self.agent.input(
                {
                    "task": plan.get("task_summary", ""),
                    "candidate_skill_cards": cards,
                    "semantic_output_contract": semantic_output_contract,
                    "planner_requirements": [
                        "Select entry and supporting skills from candidate_skill_cards only.",
                        "Switch skills by task stage when the case needs domain planning, tools, artifacts, QA, approval, or fallback.",
                        "Represent intermediate artifacts and which later stage consumes them.",
                        "Separate Skill guidance, Actions/tools, external APIs/MCP/SaaS, and final artifacts.",
                        "If side effects or credentials are involved, include explicit approval gates.",
                        "If dependencies, APIs, files, or environments may fail, include retry/fallback/degraded-mode behavior.",
                        "Cover every required semantic deliverable by role and type, not only by filename.",
                    ],
                }
            )
            .instruct(
                "Produce a Skills Executor orchestration plan. Do not claim that files or external writes are already complete; "
                "describe the executable plan and required boundaries."
            )
            .output(output_schema)
        )
        result = await request.async_start(max_retries=max(1, max_revisions), raise_ensure_failure=False)
        return dict(result) if isinstance(result, dict) else {}

    async def async_execute_action(
        self,
        action_id: str,
        kwargs: dict[str, Any],
        *,
        purpose: str,
        source_protocol: str,
    ) -> ActionResult:
        return await self.agent.action.async_execute_action(
            action_id,
            kwargs,
            purpose=purpose,
            source_protocol=source_protocol,
        )


def create_agent_skills_runtime_context(agent: Any) -> SkillsRuntimeContext:
    return AgentSkillsRuntimeContext(agent)
