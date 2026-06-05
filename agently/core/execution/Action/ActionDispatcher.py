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

import asyncio
import json
from typing import Any, cast

from agently.core.execution.ExecutionEnvironment import (
    ExecutionEnvironmentApprovalDenied,
    ExecutionEnvironmentApprovalRequired,
    ExecutionEnvironmentError,
)
from agently.types.data import (
    ActionApproval,
    ActionCall,
    ActionPolicy,
    ActionResult,
    ActionSpec,
    ExecutionEnvironmentHandle,
    ExecutionEnvironmentPolicy,
    ExecutionEnvironmentRequirement,
)
from agently.types.plugins import ActionExecutor
from agently.utils import FunctionShifter, Settings, SettingsNamespace

from .ActionRegistry import ActionRegistry


class ActionDispatcher:
    VALID_STATUSES = {"success", "partial_success", "error", "approval_required", "blocked", "skipped"}

    def __init__(self, registry: ActionRegistry, settings: Settings):
        self.registry = registry
        self.settings = settings

    @staticmethod
    def _to_dict(value: Any):
        return value if isinstance(value, dict) else {}

    def _merge_policy(
        self,
        settings: Settings,
        spec: ActionSpec,
        policy_override: ActionPolicy | None = None,
    ) -> ActionPolicy:
        action_settings = SettingsNamespace(settings, "action")
        merged: dict[str, Any] = {}
        for candidate in (
            action_settings.get("policy.global", {}),
            action_settings.get("policy.agent", action_settings.get("policy", {})),
            spec.get("default_policy", {}),
            policy_override or {},
        ):
            if isinstance(candidate, dict):
                merged.update(cast(dict[str, Any], candidate))
        return cast(ActionPolicy, merged)

    def _approval_result(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        policy: ActionPolicy,
        reason: str,
        message: str,
    ) -> ActionResult:
        action_id = str(spec.get("action_id", ""))
        tool_name = str(spec.get("name", action_id))
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        approval: ActionApproval = {
            "required": True,
            "reason": reason,
            "approval_mode": str(policy.get("approval_mode", "auto")),
            "suggested_policy": policy,
            "message": message,
        }
        result: dict[str, Any] = {
            "ok": False,
            "status": "approval_required",
            "purpose": str(action_call.get("purpose", f"Use { action_id }")),
            "action_id": action_id,
            "tool_name": tool_name,
            "kwargs": dict(action_input),
            "todo_suggestion": str(action_call.get("todo_suggestion", "")),
            "next": str(action_call.get("next", action_call.get("todo_suggestion", ""))),
            "success": False,
            "result": None,
            "data": None,
            "approval": approval,
            "error": message,
            "expose_to_model": bool(spec.get("expose_to_model", True)),
            "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
            "executor_type": str(spec.get("executor_type", "")),
        }
        return cast(ActionResult, result)

    async def _resolve_action_approval(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        policy: ActionPolicy,
    ) -> tuple[bool, ActionPolicy | None, ActionResult | None]:
        if policy.get("policy_approval_granted") is True:
            return True, policy, None

        from agently.base import policy_approval

        action_id = str(spec.get("action_id", ""))
        decision = await policy_approval.async_resolve(
            {
                "source": "action",
                "capability": action_id,
                "subject": str(spec.get("name") or action_id),
                "risk": str(spec.get("side_effect_level", "")),
                "payload": {
                    "action_call": dict(action_call),
                    "action_spec": {
                        "action_id": action_id,
                        "name": str(spec.get("name", action_id)),
                        "side_effect_level": str(spec.get("side_effect_level", "")),
                        "executor_type": str(spec.get("executor_type", "")),
                    },
                },
                "policy": dict(policy),
            },
            handler=str(policy.get("policy_approval_handler") or "") or None,
        )
        status = str(decision.get("status", "pending"))
        if status == "approved":
            merged_policy = cast(ActionPolicy, dict(policy))
            override = decision.get("policy_override", {})
            if isinstance(override, dict):
                cast(dict[str, Any], merged_policy).update(override)
            merged_policy["policy_approval_granted"] = True
            merged_policy["policy_approval_decision"] = dict(decision)
            return True, merged_policy, None
        message = str(decision.get("reason") or f"Action '{ action_id }' requires approval before execution.")
        result = self._approval_result(
            spec=spec,
            action_call=action_call,
            policy=policy,
            reason=status,
            message=message,
        )
        approval = result.get("approval", {})
        if isinstance(approval, dict):
            approval["decision"] = dict(decision)
            result["approval"] = cast(ActionApproval, approval)
        if status == "denied":
            result["status"] = "blocked"
            result["error"] = message
        return False, None, result

    def _normalize_executor_output(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        output: Any,
        policy: ActionPolicy,
    ) -> ActionResult:
        action_id = str(spec.get("action_id", ""))
        tool_name = str(spec.get("name", action_id))
        if isinstance(output, dict) and output.get("status") in self.VALID_STATUSES:
            result: dict[str, Any] = dict(output)
        elif isinstance(output, dict) and output.get("need_approval") is True:
            message = str(output.get("reason", "Action execution requires approval."))
            result = dict(
                self._approval_result(
                    spec=spec,
                    action_call=action_call,
                    policy=policy,
                    reason=message,
                    message=message,
                )
            )
        else:
            status = "success"
            error = ""
            if isinstance(output, str) and output.strip().startswith("Error:"):
                status = "error"
                error = output
            elif isinstance(output, dict) and output.get("ok") is False:
                status = "error"
                error = str(output.get("reason", output.get("stderr", "Action execution failed.")))
            elif isinstance(output, dict) and isinstance(output.get("error"), str) and output.get("error"):
                status = "error"
                error = str(output["error"])

            result = {
                "ok": status == "success",
                "status": cast(Any, status),
                "data": output,
                "result": output,
                "error": error,
            }

        purpose = str(action_call.get("purpose", f"Use { action_id }"))
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        result.setdefault("ok", result.get("status") in {"success", "partial_success"})
        result.setdefault("status", "success" if result.get("ok") else "error")
        result.setdefault("purpose", purpose)
        result.setdefault("action_id", action_id)
        result.setdefault("tool_name", tool_name)
        result.setdefault("kwargs", dict(action_input))
        result.setdefault("todo_suggestion", str(action_call.get("todo_suggestion", "")))
        result.setdefault("next", str(action_call.get("next", action_call.get("todo_suggestion", ""))))
        result.setdefault("result", result.get("data"))
        result.setdefault("data", result.get("result"))
        result.setdefault("success", result.get("status") in {"success", "partial_success"})
        result.setdefault("artifacts", [])
        result.setdefault("diagnostics", [])
        result.setdefault("meta", {})
        result.setdefault("approval", {})
        result.setdefault("error", "")
        result.setdefault("expose_to_model", bool(spec.get("expose_to_model", True)))
        result.setdefault("side_effect_level", cast(Any, spec.get("side_effect_level", "read")))
        result.setdefault("executor_type", str(spec.get("executor_type", "")))
        return cast(ActionResult, result)

    @staticmethod
    def _to_execution_environment_policy(policy: ActionPolicy) -> ExecutionEnvironmentPolicy:
        keys = {
            "approval_mode",
            "policy_approval_handler",
            "workspace_roots",
            "path_allowlist",
            "path_denylist",
            "allowed_cmd_prefixes",
            "network_mode",
            "timeout_seconds",
            "max_output_bytes",
            "read_only",
            "allow_create",
            "allow_update",
            "allow_delete",
        }
        return cast(ExecutionEnvironmentPolicy, {key: policy[key] for key in keys if key in policy})

    def _resolve_execution_environment_owner_id(
        self,
        settings: Settings,
        requirement: ExecutionEnvironmentRequirement,
    ):
        if requirement.get("owner_id"):
            return str(requirement.get("owner_id", ""))
        configured = settings.get("execution_environment.owner_id", None)
        if isinstance(configured, str) and configured:
            return configured
        session_id = settings.get("runtime.session_id", None)
        if isinstance(session_id, str) and session_id:
            return session_id
        return self.registry.name or "Action"

    def _prepare_execution_environment_requirements(
        self,
        *,
        spec: ActionSpec,
        settings: Settings,
        policy: ActionPolicy,
    ):
        requirements = spec.get("execution_environments", [])
        if not isinstance(requirements, list):
            return []
        prepared: list[ExecutionEnvironmentRequirement] = []
        action_policy = self._to_execution_environment_policy(policy)
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            prepared_requirement = cast(ExecutionEnvironmentRequirement, dict(requirement))
            requirement_policy = dict(prepared_requirement.get("policy", {}))
            requirement_policy.update(action_policy)
            prepared_requirement["policy"] = cast(ExecutionEnvironmentPolicy, requirement_policy)
            prepared_requirement.setdefault("scope", "action_call")
            prepared_requirement.setdefault("owner_id", self._resolve_execution_environment_owner_id(settings, prepared_requirement))
            prepared_requirement.setdefault("resource_key", str(spec.get("action_id", prepared_requirement.get("kind", ""))))
            prepared.append(prepared_requirement)
        return prepared

    def _execution_environment_error_result(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        status: str,
        error: str,
        approval: ActionApproval | None = None,
    ) -> ActionResult:
        action_id = str(spec.get("action_id", ""))
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        return cast(ActionResult, {
            "ok": False,
            "status": status,
            "purpose": str(action_call.get("purpose", f"Use { action_id }")),
            "action_id": action_id,
            "tool_name": str(spec.get("name", action_id)),
            "kwargs": dict(action_input),
            "todo_suggestion": str(action_call.get("todo_suggestion", "")),
            "next": str(action_call.get("next", action_call.get("todo_suggestion", ""))),
            "success": False,
            "result": None,
            "data": None,
            "approval": approval or {},
            "error": error,
            "expose_to_model": bool(spec.get("expose_to_model", True)),
            "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
            "executor_type": str(spec.get("executor_type", "")),
        })

    async def async_execute(
        self,
        action_id: str,
        action_input: dict[str, Any],
        *,
        settings: Settings | None = None,
        purpose: str | None = None,
        policy_override: ActionPolicy | None = None,
        source_protocol: str = "direct",
        todo_suggestion: str = "",
        next_value: str = "",
    ) -> ActionResult:
        execution_settings = settings if settings is not None else self.settings
        spec = self.registry.get_spec(action_id)
        if spec is None:
            return {
                "ok": False,
                "status": "error",
                "purpose": purpose or f"Use { action_id }",
                "action_id": action_id,
                "tool_name": action_id,
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Can not find action named '{ action_id }'",
                "expose_to_model": False,
                "side_effect_level": "read",
                "executor_type": "",
            }

        executor = self.registry.get_executor(action_id)
        tool_name = str(spec.get("name", action_id))
        if executor is None:
            return {
                "ok": False,
                "status": "error",
                "purpose": purpose or f"Use { action_id }",
                "action_id": action_id,
                "tool_name": tool_name,
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"No executor registered for action '{ action_id }'",
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }

        action_call: ActionCall = {
            "purpose": purpose or f"Use { action_id }",
            "action_id": action_id,
            "action_input": dict(action_input),
            "policy_override": policy_override or {},
            "source_protocol": source_protocol,
            "todo_suggestion": todo_suggestion,
            "next": next_value or todo_suggestion,
            "tool_name": str(spec.get("name", action_id)),
            "tool_kwargs": dict(action_input),
        }
        policy = self._merge_policy(execution_settings, spec, policy_override)
        policy_approval_handler = execution_settings.get("policy_approval.handler", None)
        if policy_approval_handler is not None and not policy.get("policy_approval_handler"):
            policy["policy_approval_handler"] = str(policy_approval_handler)

        if spec.get("approval_required") is True or policy.get("approval_mode") == "always":
            approved, approved_policy, approval_result = await self._resolve_action_approval(
                spec=spec,
                action_call=action_call,
                policy=policy,
            )
            if not approved:
                return cast(ActionResult, approval_result)
            if approved_policy is not None:
                policy = approved_policy
        if spec.get("sandbox_required") is True and not getattr(executor, "sandboxed", False):
            return {
                "ok": False,
                "status": "blocked",
                "purpose": str(action_call["purpose"]),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Action '{ action_id }' requires a sandboxed executor.",
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }

        from agently.base import execution_environment

        ensured_handles: list[ExecutionEnvironmentHandle] = []
        environment_resources: dict[str, Any] = {}
        environment_handles: dict[str, ExecutionEnvironmentHandle] = {}
        try:
            for requirement in self._prepare_execution_environment_requirements(
                spec=spec,
                settings=execution_settings,
                policy=policy,
            ):
                handle = await execution_environment.async_ensure(
                    requirement,
                    owner_id=str(requirement.get("owner_id", "")),
                )
                ensured_handles.append(handle)
                resource_key = str(handle.get("resource_key", requirement.get("resource_key", "")))
                if resource_key:
                    environment_handles[resource_key] = handle
                    environment_resources[resource_key] = handle.get("resource")
            if environment_handles:
                action_call["execution_environment_handles"] = environment_handles
                action_call["execution_environment_resources"] = environment_resources
        except ExecutionEnvironmentApprovalRequired as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            approval: ActionApproval = {
                "required": True,
                "reason": error.code,
                "approval_mode": str(error.payload.get("policy", {}).get("approval_mode", "auto")),
                "missing_permissions": [error.code],
                "suggested_policy": cast(ActionPolicy, error.payload.get("policy", {})),
                "message": str(error),
            }
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="approval_required",
                error=str(error),
                approval=approval,
            )
        except ExecutionEnvironmentApprovalDenied as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="blocked",
                error=str(error),
            )
        except ExecutionEnvironmentError as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=str(error),
            )
        except Exception as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=str(error),
            )

        timeout = policy.get("timeout_seconds", None)
        timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else 0.0
        try:
            if isinstance(timeout, (int, float)) and timeout > 0:
                output = await asyncio.wait_for(
                    executor.execute(
                        spec=spec,
                        action_call=action_call,
                        policy=policy,
                        settings=execution_settings,
                    ),
                    timeout=float(timeout),
                )
            else:
                output = await executor.execute(
                    spec=spec,
                    action_call=action_call,
                    policy=policy,
                    settings=execution_settings,
                )
        except asyncio.TimeoutError:
            for handle in ensured_handles:
                if handle.get("scope") == "action_call":
                    await execution_environment.async_release(handle)
            return {
                "ok": False,
                "status": "error",
                "purpose": str(action_call["purpose"]),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Action '{ action_id }' timed out after { timeout_seconds } seconds.",
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }
        except Exception as error:
            for handle in ensured_handles:
                if handle.get("scope") == "action_call":
                    await execution_environment.async_release(handle)
            return {
                "ok": False,
                "status": "error",
                "purpose": str(action_call["purpose"]),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": str(error),
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }
        finally:
            for handle in ensured_handles:
                if handle.get("scope") == "action_call":
                    await execution_environment.async_release(handle)

        result = self._normalize_executor_output(
            spec=spec,
            action_call=action_call,
            output=output,
            policy=policy,
        )
        max_output_bytes = policy.get("max_output_bytes")
        if isinstance(max_output_bytes, int) and max_output_bytes > 0:
            serialized = json.dumps(result.get("data"), ensure_ascii=False, default=str)
            if len(serialized.encode("utf-8")) > max_output_bytes:
                truncated = serialized.encode("utf-8")[:max_output_bytes].decode("utf-8", errors="ignore")
                result["data"] = truncated
                result["result"] = truncated
                result_meta = result.get("meta")
                if not isinstance(result_meta, dict):
                    result_meta = {}
                result_meta["truncated"] = True
                result["meta"] = result_meta
        return result

    def execute(self, action_id: str, action_input: dict[str, Any], **kwargs):
        return FunctionShifter.syncify(self.async_execute)(action_id, action_input, **kwargs)

    async def async_dry_run(
        self,
        action_id: str,
        action_input: dict[str, Any],
        *,
        settings: Settings | None = None,
        policy_override: ActionPolicy | None = None,
    ) -> ActionResult:
        return await self.async_execute(
            action_id,
            action_input,
            settings=settings,
            purpose=f"Dry run { action_id }",
            policy_override=policy_override,
            source_protocol="dry_run",
        )

    def dry_run(self, action_id: str, action_input: dict[str, Any], **kwargs):
        return FunctionShifter.syncify(self.async_dry_run)(action_id, action_input, **kwargs)
