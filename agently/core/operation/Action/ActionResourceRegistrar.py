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

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.types.data import (
    ActionPolicy,
    ExecutionResourcePolicy,
    ExecutionResourceRequirement,
)
from agently.utils import DataFormatter, LazyImport
from agently.utils.MCP import normalize_mcp_transport

if TYPE_CHECKING:
    from agently.types.data import MCPConfigs
    from .Action import Action


class ActionResourceRegistrar:
    def __init__(self, action: "Action"):
        self._action = action

    @staticmethod
    def _format_bash_sandbox_desc(
        desc: str,
        *,
        allowed_cmd_prefixes: list[str] | None,
        allowed_workdir_roots: list[str | Path] | None,
        timeout: int,
        max_output_chars: int | None = None,
    ) -> str:
        command_text = (
            ", ".join(str(prefix) for prefix in allowed_cmd_prefixes)
            if allowed_cmd_prefixes
            else "executor default safe profile"
        )
        roots_text = (
            ", ".join(str(Path(root)) for root in allowed_workdir_roots)
            if allowed_workdir_roots
            else "no Workspace root configured"
        )
        policy_desc = (
            f"Allowed command prefixes: {command_text}. "
            f"Allowed working directory roots: {roots_text}. "
            f"Timeout: {timeout} seconds."
        )
        if max_output_chars is not None:
            policy_desc += f" Output preview limit: {max_output_chars} characters per stream."
        base_desc = str(desc).strip()
        return f"{base_desc}\n\n{policy_desc}" if base_desc else policy_desc

    async def async_use_action_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        side_effect_level: Literal["read", "write", "exec"] = "read",
        approval_required: bool = False,
        sandbox_required: bool = False,
        replay_safe: bool = True,
        expose_to_model: bool = True,
    ):
        action = self._action
        LazyImport.import_package("fastmcp", version_constraint=">=3", auto_install=False)
        from fastmcp import Client  # pyright: ignore[reportMissingImports]

        transport = normalize_mcp_transport(transport, headers=headers)
        normalized_tags = action._normalize_tags(tags)

        async with Client(transport) as client:  # type: ignore[arg-type]
            tool_list = await client.list_tools()
            for tool in tool_list:
                tool_tags = []
                if hasattr(tool, "_meta") and tool._meta:  # type: ignore[attr-defined]
                    tool_tags = tool._meta.get("_fastmcp", {}).get("tags", [])  # type: ignore[index]
                tool_tags.extend(normalized_tags)
                action.register_action(
                    action_id=tool.name,
                    desc=tool.description,
                    kwargs=DataFormatter.from_schema_to_kwargs_format(tool.inputSchema),
                    returns=DataFormatter.from_schema_to_kwargs_format(tool.outputSchema),
                    executor=action._create_executor(
                        "MCPActionExecutor",
                        action_id=tool.name,
                        transport=transport,
                    ),
                    tags=tool_tags,
                    default_policy=default_policy,
                    side_effect_level=side_effect_level,
                    approval_required=approval_required,
                    sandbox_required=sandbox_required,
                    replay_safe=replay_safe,
                    expose_to_model=expose_to_model,
                    execution_resources=cast(list[ExecutionResourceRequirement], [
                        {
                            "requirement_id": f"mcp:{ tool.name }",
                            "kind": "mcp",
                            "scope": "agent",
                            "resource_key": tool.name,
                            "config": {"transport": transport},
                            "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                            "approval_required": approval_required,
                        }
                    ]),
                )
        return action

    async def async_use_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
    ):
        await self.async_use_action_mcp(transport, headers=headers, tags=tags)
        return self._action

    def register_python_sandbox_action(
        self,
        *,
        action_id: str = "python_sandbox",
        desc: str = "Execute Python code inside a restricted sandbox.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        preset_objects: dict[str, object] | None = None,
        base_vars: dict[str, Any] | None = None,
        allowed_return_types: list[type] | None = None,
    ):
        action = self._action
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={"python_code": (str, "Python code to execute in the sandbox.")},
            executor=action._create_executor(
                "PythonSandboxActionExecutor",
                preset_objects=preset_objects,
                base_vars=base_vars,
                allowed_return_types=allowed_return_types,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"python:{ action_id }",
                    "kind": "python",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "preset_objects": preset_objects,
                        "base_vars": base_vars,
                        "allowed_return_types": allowed_return_types,
                    },
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                }
            ]),
        )
        return action

    def register_bash_sandbox_action(
        self,
        *,
        action_id: str = "bash_sandbox",
        desc: str = "Execute a shell command inside a constrained sandbox.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        allowed_cmd_prefixes: list[str] | None = None,
        allowed_workdir_roots: list[str | Path] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        max_output_chars: int = 20000,
        output_artifact_dir: str | Path | None = None,
    ):
        action = self._action
        model_desc = self._format_bash_sandbox_desc(
            desc,
            allowed_cmd_prefixes=allowed_cmd_prefixes,
            allowed_workdir_roots=allowed_workdir_roots,
            timeout=timeout,
            max_output_chars=max_output_chars,
        )
        action.register_action(
            action_id=action_id,
            desc=model_desc,
            kwargs={
                "cmd": ("str | list[str]", "Command to run inside the sandbox."),
                "workdir": ("str | None", "Working directory inside allowed roots."),
                "allow_unsafe": ("bool", "Bypass the command allowlist."),
            },
            executor=action._create_executor(
                "BashSandboxActionExecutor",
                allowed_cmd_prefixes=allowed_cmd_prefixes,
                allowed_workdir_roots=allowed_workdir_roots,
                timeout=timeout,
                env=env,
                max_output_chars=max_output_chars,
                output_artifact_dir=output_artifact_dir,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"bash:{ action_id }",
                    "kind": "bash",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "allowed_cmd_prefixes": allowed_cmd_prefixes,
                        "allowed_workdir_roots": allowed_workdir_roots,
                        "timeout": timeout,
                        "env": env,
                        "max_output_chars": max_output_chars,
                        "output_artifact_dir": output_artifact_dir,
                    },
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                }
            ]),
        )
        return action

    def register_nodejs_action(
        self,
        *,
        action_id: str = "run_nodejs",
        desc: str = "Execute JavaScript with Node.js inside a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        node_binary: str = "node",
        cwd: str | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
    ):
        action = self._action
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "js_code": (str, "JavaScript code to execute with Node.js."),
                "args": ("list[str]", "Optional command-line arguments."),
            },
            executor=action._create_executor("NodeJSActionExecutor", timeout=timeout),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"node:{ action_id }",
                    "kind": "node",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "node_binary": node_binary,
                        "cwd": cwd,
                        "timeout": timeout,
                        "env": env,
                    },
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                }
            ]),
        )
        return action

    def register_docker_action(
        self,
        *,
        action_id: str = "run_docker",
        desc: str = "Run a command in a Docker container through a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        image: str | None = None,
        timeout: int = 60,
        docker_binary: str = "docker",
        default_args: list[str] | None = None,
    ):
        action = self._action
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "image": ("str | None", "Docker image. Defaults to the configured image."),
                "cmd": ("str | list[str]", "Command to run in the container."),
                "workdir": ("str | None", "Container working directory."),
                "env": ("dict[str, str] | None", "Container environment variables."),
            },
            executor=action._create_executor("DockerActionExecutor", image=image, timeout=timeout),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            approval_required=True,
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"docker:{ action_id }",
                    "kind": "docker",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "docker_binary": docker_binary,
                        "timeout": timeout,
                        "default_args": default_args or [],
                    },
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                    "approval_required": True,
                }
            ]),
        )
        return action

    def register_sqlite_action(
        self,
        *,
        action_id: str = "query_sqlite",
        desc: str = "Query a SQLite database through a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        database: str = ":memory:",
        read_only: bool = True,
        uri: bool = False,
    ):
        action = self._action
        merged_policy: ActionPolicy = cast(ActionPolicy, dict(default_policy or {}))
        merged_policy.setdefault("read_only", read_only)
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "query": (str, "SQLite query to execute."),
                "params": ("list | dict | None", "Optional SQLite query parameters."),
            },
            executor=action._create_executor("SQLiteActionExecutor", read_only=read_only),
            tags=tags,
            default_policy=merged_policy,
            side_effect_level="read" if read_only else "write",
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"sqlite:{ action_id }",
                    "kind": "sqlite",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "database": database,
                        "uri": uri,
                    },
                    "policy": cast(ExecutionResourcePolicy, merged_policy),
                }
            ]),
        )
        return action
