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
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from agently.types.data import ExecutionResourceRequirement
from agently.utils import FunctionShifter, LazyImport


async def _await_value(value: Awaitable[Any]) -> Any:
    return await value


def _resolve_sync(value: Any) -> Any:
    if inspect.isawaitable(value):
        return FunctionShifter.syncify(_await_value)(cast(Awaitable[Any], value))
    return value


@runtime_checkable
class ACPProvider(Protocol):
    def discover_agents(
        self,
        *,
        root: str,
        agent_ids: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any] | Iterable[Mapping[str, Any]]: ...

    async def async_run_task(
        self,
        *,
        agent_id: str,
        task: str,
        root: str,
        working_dir: str,
        timeout_seconds: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | str: ...


class LocalACPProvider:
    COMMON_AGENT_COMMANDS = ("codex", "claude", "gemini")

    def discover_agents(
        self,
        *,
        root: str,
        agent_ids: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        requested = [str(item).strip() for item in (agent_ids or self.COMMON_AGENT_COMMANDS) if str(item).strip()]
        command_candidates = [
            {"agent_id": item, "command": item, "available": shutil.which(item) is not None}
            for item in requested
        ]
        try:
            acp_module = LazyImport.import_package("acp", auto_install=False)
        except ImportError as error:
            return {
                "agents": [],
                "diagnostics": [
                    {
                        "code": "acp.dependency_missing",
                        "message": str(error),
                        "requested_agent_ids": requested,
                        "command_candidates": command_candidates,
                    }
                ],
            }

        discover = getattr(acp_module, "discover_agents", None)
        if not callable(discover):
            return {
                "agents": [],
                "diagnostics": [
                    {
                        "code": "acp.discovery_api_missing",
                        "message": "Installed acp package does not expose discover_agents(...).",
                        "requested_agent_ids": requested,
                        "command_candidates": command_candidates,
                    }
                ],
            }
        result = _resolve_sync(discover(root=root, agent_ids=requested or None, timeout_seconds=timeout_seconds))
        if isinstance(result, Mapping):
            return result
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
            return {"agents": list(result), "diagnostics": []}
        return {"agents": [], "diagnostics": [{"code": "acp.discovery_invalid_result"}]}

    async def async_run_task(
        self,
        *,
        agent_id: str,
        task: str,
        root: str,
        working_dir: str,
        timeout_seconds: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        acp_module = LazyImport.import_package("acp", auto_install=False)
        run_task = getattr(acp_module, "run_task", None)
        if not callable(run_task):
            return {
                "ok": False,
                "status": "error",
                "error": "Installed acp package does not expose run_task(...).",
                "diagnostics": [{"code": "acp.run_api_missing"}],
            }
        result = run_task(
            agent_id=agent_id,
            task=task,
            root=root,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            context=dict(context or {}),
        )
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, Mapping) else {"output": str(result)}


class ACP:
    def __init__(
        self,
        *,
        root: str | Path | None = None,
        agent_ids: list[str] | tuple[str, ...] | str | None = None,
        provider: ACPProvider | Any | None = None,
        on_missing: Literal["skip", "error"] = "skip",
        timeout_seconds: float | None = 600,
        action_prefix: str = "",
    ):
        self.root = Path(root or Path.cwd()).resolve()
        self.agent_ids = self._normalize_agent_ids(agent_ids)
        self.provider = provider if provider is not None else LocalACPProvider()
        self.on_missing = on_missing
        self.timeout_seconds = timeout_seconds
        self.action_prefix = action_prefix.strip()
        self._agents: list[dict[str, Any]] | None = None
        self._diagnostics: list[dict[str, Any]] = []

    @staticmethod
    def _normalize_agent_ids(value: list[str] | tuple[str, ...] | str | None) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            values = [value]
        else:
            values = list(value)
        normalized = [str(item).strip() for item in values if str(item).strip()]
        return normalized or None

    @staticmethod
    def _normalize_agent(agent: Mapping[str, Any]) -> dict[str, Any]:
        agent_id = str(agent.get("agent_id") or agent.get("id") or agent.get("name") or "").strip()
        return {
            "agent_id": agent_id,
            "name": str(agent.get("name") or agent_id),
            "status": str(agent.get("status") or "ready"),
            "endpoint": str(agent.get("endpoint") or ""),
            "command": str(agent.get("command") or ""),
            "meta": dict(agent.get("meta", {})) if isinstance(agent.get("meta"), Mapping) else {},
        }

    def _discover(self) -> None:
        if self._agents is not None:
            return
        self._agents = []
        self._diagnostics = []
        discover = getattr(self.provider, "discover_agents", None)
        if not callable(discover):
            self._diagnostics.append(
                {
                    "code": "acp.provider_invalid",
                    "message": "ACP provider must expose discover_agents(...).",
                }
            )
        else:
            try:
                result = discover(
                    root=str(self.root),
                    agent_ids=self.agent_ids,
                    timeout_seconds=self.timeout_seconds,
                )
                result = _resolve_sync(result)
                if isinstance(result, Mapping):
                    agents = result.get("agents", [])
                    diagnostics = result.get("diagnostics", [])
                elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
                    agents = list(result or [])
                    diagnostics = []
                else:
                    agents = []
                    diagnostics = [{"code": "acp.discovery_invalid_result"}]
                if isinstance(diagnostics, list):
                    self._diagnostics.extend(
                        dict(item) if isinstance(item, Mapping) else {"message": str(item)}
                        for item in diagnostics
                    )
                for agent in agents if isinstance(agents, Iterable) else []:
                    if not isinstance(agent, Mapping):
                        continue
                    normalized = self._normalize_agent(agent)
                    if normalized["agent_id"] and normalized["status"] in {"ready", "success", "ok"}:
                        self._agents.append(normalized)
                    else:
                        self._diagnostics.append(
                            {
                                "code": "acp.handshake_failed",
                                "message": "ACP agent handshake did not produce a ready agent.",
                                "agent": dict(agent),
                            }
                        )
            except Exception as error:
                self._diagnostics.append(
                    {
                        "code": "acp.discovery_failed",
                        "message": str(error),
                        "exception_type": type(error).__name__,
                    }
                )

        if not self._agents and self.on_missing == "error":
            raise RuntimeError(
                "No handshake-verified Agent Client Protocol coding agent is available. "
                f"Diagnostics: { self._diagnostics }"
            )

    def _action_name(self, name: str) -> str:
        return f"{ self.action_prefix }{ name }" if self.action_prefix else name

    def _agents_by_id(self) -> dict[str, dict[str, Any]]:
        self._discover()
        return {str(agent.get("agent_id")): agent for agent in self._agents or []}

    def _resource_requirement(self) -> ExecutionResourceRequirement:
        return {
            "kind": "acp",
            "scope": "action_call",
            "resource_key": f"acp:{ self.root }",
            "config": {
                "root": str(self.root),
                "agents": list(self._agents or []),
            },
            "policy": {
                "workspace_roots": [str(self.root)],
                "timeout_seconds": float(self.timeout_seconds or 0),
            },
            "meta": {"component": "builtins.actions.ACP"},
        }

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_prefix: str = "",
        expose_to_model: bool = True,
        default_policy: dict[str, Any] | None = None,
    ) -> list[str]:
        if action_prefix and not self.action_prefix:
            self.action_prefix = action_prefix.strip()
        self._discover()
        action_ids: list[str] = []
        list_action_id = self._action_name("acp_list_agents")
        action.register_action(
            action_id=list_action_id,
            desc="List handshake-verified local Agent Client Protocol coding agents available to this Agent.",
            kwargs={},
            func=self.list_agents,
            tags=tags,
            default_policy=default_policy,
            side_effect_level="read",
            expose_to_model=expose_to_model,
            meta={
                "component": "builtins.actions.ACP",
                "kind": "acp",
                "root": str(self.root),
                "diagnostics": list(self._diagnostics),
            },
        )
        action_ids.append(list_action_id)
        if self._agents:
            run_action_id = self._action_name("acp_run_task")
            action.register_action(
                action_id=run_action_id,
                desc=(
                    "Run a bounded coding task through one handshake-verified local Agent Client Protocol "
                    "coding agent. The agent_id must come from acp_list_agents."
                ),
                kwargs={
                    "agent_id": (str, "Handshake-verified ACP agent id from acp_list_agents."),
                    "task": (str, "Bounded coding-agent task to run inside the configured root."),
                    "working_subdir": (str, "Optional relative subdirectory under the configured root."),
                    "context": (dict, "Optional structured context for the ACP agent."),
                },
                func=self.async_run_task,
                tags=tags,
                default_policy=default_policy,
                side_effect_level="exec",
                replay_safe=False,
                expose_to_model=expose_to_model,
                execution_resources=[self._resource_requirement()],
                meta={
                    "component": "builtins.actions.ACP",
                    "kind": "acp",
                    "root": str(self.root),
                    "agent_ids": [agent["agent_id"] for agent in self._agents],
                },
            )
            action_ids.append(run_action_id)
        return action_ids

    def list_agents(self) -> dict[str, Any]:
        self._discover()
        data = {
            "agents": list(self._agents or []),
            "diagnostics": list(self._diagnostics),
            "root": str(self.root),
        }
        return {
            "ok": bool(self._agents),
            "status": "success" if self._agents else "skipped",
            "data": data,
            "result": data,
            **data,
        }

    def _resolve_working_dir(self, working_subdir: str | None = None) -> Path:
        if working_subdir is None or not str(working_subdir).strip():
            return self.root
        candidate = (self.root / str(working_subdir)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("ACP working_subdir must stay inside the configured root.")
        return candidate

    async def async_run_task(
        self,
        agent_id: str,
        task: str,
        working_subdir: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        agents = self._agents_by_id()
        selected = agents.get(str(agent_id).strip())
        if selected is None:
            return {
                "ok": False,
                "status": "error",
                "error": f"Unknown or unavailable ACP agent_id: { agent_id }.",
                "agent_id": str(agent_id),
                "available_agent_ids": sorted(agents),
                "diagnostics": [{"code": "acp.agent_unknown"}],
            }
        if not isinstance(task, str) or not task.strip():
            return {
                "ok": False,
                "status": "error",
                "error": "ACP task must be a non-empty string.",
                "agent_id": selected["agent_id"],
                "diagnostics": [{"code": "acp.task_empty"}],
            }
        try:
            working_dir = self._resolve_working_dir(working_subdir)
        except ValueError as error:
            return {
                "ok": False,
                "status": "error",
                "error": str(error),
                "agent_id": selected["agent_id"],
                "diagnostics": [{"code": "acp.root_boundary"}],
            }
        run_task = getattr(self.provider, "async_run_task", None)
        if not callable(run_task):
            return {
                "ok": False,
                "status": "error",
                "error": "ACP provider must expose async_run_task(...).",
                "agent_id": selected["agent_id"],
                "diagnostics": [{"code": "acp.provider_run_missing"}],
            }
        run_task_func = cast(
            Callable[..., Awaitable[Mapping[str, Any] | str] | Mapping[str, Any] | str],
            run_task,
        )
        result = run_task_func(
            agent_id=selected["agent_id"],
            task=task,
            root=str(self.root),
            working_dir=str(working_dir),
            timeout_seconds=self.timeout_seconds,
            context=dict(context or {}),
        )
        if inspect.isawaitable(result):
            result = await cast(Awaitable[Mapping[str, Any] | str], result)
        payload: dict[str, Any] = dict(result) if isinstance(result, Mapping) else {"output": str(result)}
        payload.setdefault("ok", payload.get("status", "success") in {"success", "partial_success"})
        payload.setdefault("status", "success" if payload.get("ok") else "error")
        payload.setdefault("agent_id", selected["agent_id"])
        payload.setdefault("root", str(self.root))
        payload.setdefault("working_dir", str(working_dir))
        payload.setdefault("diagnostics", [])
        payload_data = {
            "agent_id": payload.get("agent_id", selected["agent_id"]),
            "output": payload.get("output", payload.get("result", payload.get("data"))),
            "root": payload.get("root", str(self.root)),
            "working_dir": payload.get("working_dir", str(working_dir)),
            "diagnostics": payload.get("diagnostics", []),
        }
        payload.setdefault("data", payload_data)
        payload.setdefault("result", payload.get("data"))
        return payload
