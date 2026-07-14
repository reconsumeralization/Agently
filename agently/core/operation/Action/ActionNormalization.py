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

"""Pure normalization functions for Action decision/record handling.

All functions are stateless and import-safe — they depend only on stdlib types.
Extracted from Action.py to follow the ModelRequest/ModelRequestRunner thin-coordinator
pattern where domain logic lives in focused companion modules.
"""

from __future__ import annotations

import json
from typing import Any, cast

from agently.types.data import ActionCall, ActionDecision, ActionResult


def is_execution_error_result(result: Any) -> bool:
    if isinstance(result, dict) and isinstance(result.get("status"), str):
        return result.get("status") not in {"success", "partial_success"}
    if not isinstance(result, str):
        return False
    stripped = result.strip()
    return stripped.startswith("Error:") or stripped.startswith("Can not find tool named")


def is_next_action_path(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    normalized = path.strip()
    if normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.lstrip("./")
    return normalized == "next_action"


def parse_native_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return {}
    text = raw_arguments.strip()
    if text == "":
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"raw_arguments": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def normalize_native_action_calls(tool_call_chunks: list[Any]) -> list[ActionCall]:
    collected: dict[int, dict[str, Any]] = {}

    def _merge_one(item: Any, fallback_index: int):
        if not isinstance(item, dict):
            return
        index = item.get("index", fallback_index)
        if not isinstance(index, int):
            index = fallback_index
        current = collected.setdefault(
            index,
            {
                "id": item.get("id"),
                "type": item.get("type", "function"),
                "function": {"name": "", "arguments": ""},
            },
        )
        if item.get("id"):
            current["id"] = item["id"]
        if item.get("type"):
            current["type"] = item["type"]
        function = item.get("function", {})
        if isinstance(function, dict):
            current_function = current.setdefault("function", {"name": "", "arguments": ""})
            name = function.get("name")
            if isinstance(name, str) and name:
                current_function["name"] = name if not current_function.get("name") else current_function["name"] + name
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                current_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif isinstance(arguments, str):
                current_function["arguments"] = str(current_function.get("arguments", "")) + arguments

    for chunk in tool_call_chunks:
        if isinstance(chunk, list):
            for index, item in enumerate(chunk):
                _merge_one(item, index)
        else:
            _merge_one(chunk, len(collected))

    action_calls: list[ActionCall] = []
    for index in sorted(collected.keys()):
        function = collected[index].get("function", {})
        action_id = function.get("name")
        if not isinstance(action_id, str) or action_id.strip() == "":
            continue
        parsed_arguments = parse_native_arguments(function.get("arguments", ""))
        action_calls.append(
            {
                "purpose": f"Use {action_id}",
                "action_id": action_id,
                "action_input": parsed_arguments,
                "policy_override": {},
                "source_protocol": "native_tool_calls",
                "todo_suggestion": "",
                "next": "",
                "tool_name": action_id,
                "tool_kwargs": parsed_arguments,
            }
        )
    return action_calls


def normalize_action_call(
    command: Any,
    *,
    fallback_next: str | None = None,
) -> ActionCall | None:
    if not isinstance(command, dict):
        return None

    action_id = command.get("action_id")
    if not isinstance(action_id, str) or action_id.strip() == "":
        action_id = command.get("tool_name")
    if not isinstance(action_id, str) or action_id.strip() == "":
        return None

    purpose = command.get("purpose")
    if not isinstance(purpose, str) or purpose.strip() == "":
        purpose = f"Use {action_id}"

    action_input = command.get("action_input", command.get("tool_kwargs", {}))
    if not isinstance(action_input, dict):
        action_input = {}

    policy_override = command.get("policy_override", {})
    if not isinstance(policy_override, dict):
        policy_override = {}

    command_next = command.get("todo_suggestion")
    if not isinstance(command_next, str) or command_next.strip() == "":
        command_next = command.get("next")
    if not isinstance(command_next, str) or command_next.strip() == "":
        command_next = fallback_next if isinstance(fallback_next, str) and fallback_next.strip() != "" else ""

    action_call: dict[str, Any] = {
        "purpose": purpose,
        "action_id": action_id,
        "action_input": action_input,
        "policy_override": policy_override,
        "source_protocol": str(command.get("source_protocol", "structured_plan")),
        "todo_suggestion": command_next,
        "next": command_next,
        "tool_name": action_id,
        "tool_kwargs": action_input,
    }
    return cast(ActionCall, action_call)


def normalize_action_decision(decision: Any) -> ActionDecision:
    if not isinstance(decision, dict):
        return {
            "next_action": "response",
            "use_action": False,
            "next": "",
            "action_calls": [],
            "diagnostics": [],
        }

    fallback_next = decision.get("todo_suggestion")
    if not isinstance(fallback_next, str):
        fallback_next = decision.get("next")
    if not isinstance(fallback_next, str):
        fallback_next = ""

    action_calls: list[ActionCall] = []
    command_key: str | None = None
    for key in ("execution_actions", "action_calls", "execution_commands", "tool_commands"):
        if isinstance(decision.get(key), list):
            command_key = key
            break
    if command_key:
        for command in decision[command_key]:
            action_call = normalize_action_call(command, fallback_next=fallback_next)
            if action_call is not None:
                action_calls.append(action_call)

    if len(action_calls) == 0:
        for single_key in ("action_call", "tool_command"):
            if single_key in decision:
                action_call = normalize_action_call(decision.get(single_key), fallback_next=fallback_next)
                if action_call is not None:
                    action_calls.append(action_call)
                    break

    next_action = decision.get("next_action")
    if not isinstance(next_action, str) or next_action.strip() == "":
        next_action = "execute" if len(action_calls) > 0 else "response"
    next_action = next_action.lower()
    if next_action not in {"execute", "response"}:
        next_action = "execute" if len(action_calls) > 0 else "response"

    use_action = decision.get("use_action")
    if not isinstance(use_action, bool):
        use_action = decision.get("use_tool")
    if isinstance(use_action, bool):
        final_use_action = use_action and len(action_calls) > 0 and next_action == "execute"
    else:
        final_use_action = len(action_calls) > 0 and next_action == "execute"

    if not final_use_action:
        action_calls = []
        next_action = "response"

    diagnostics = decision.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []

    return {
        "next_action": next_action,
        "use_action": final_use_action,
        "next": fallback_next,
        "execution_actions": action_calls,
        "action_calls": action_calls,
        "execution_commands": action_calls,
        "tool_commands": action_calls,
        "diagnostics": diagnostics,
    }


def normalize_execution_record(
    record: Any,
    command: ActionCall | None,
    index: int,
) -> ActionResult:
    if command is None:
        command = {}

    fallback_action_id = str(command.get("action_id", command.get("tool_name", "")))
    fallback_kwargs = command.get("action_input", command.get("tool_kwargs", {}))
    if not isinstance(fallback_kwargs, dict):
        fallback_kwargs = {}
    fallback_purpose = str(command.get("purpose", f"action_call_{index + 1}"))
    fallback_next = str(command.get("todo_suggestion", command.get("next", "")))

    if isinstance(record, dict):
        action_id = record.get("action_id", fallback_action_id)
        if not isinstance(action_id, str):
            action_id = fallback_action_id

        kwargs = record.get("kwargs", fallback_kwargs)
        if not isinstance(kwargs, dict):
            kwargs = fallback_kwargs

        purpose = record.get("purpose", fallback_purpose)
        if not isinstance(purpose, str):
            purpose = fallback_purpose

        next_step = record.get("todo_suggestion", record.get("next", fallback_next))
        if not isinstance(next_step, str):
            next_step = fallback_next

        result = record.get("result", record.get("data"))
        error = record.get("error", "")
        if not isinstance(error, str):
            error = str(error)

        nested_status = result.get("status") if isinstance(result, dict) else None
        default_status = "success" if error == "" else "error"
        if isinstance(nested_status, str) and nested_status:
            default_status = nested_status
        status = record.get("status", default_status)
        if not isinstance(status, str):
            status = "success" if error == "" else "error"

        success = record.get("success")
        if not isinstance(success, bool):
            success = status in {"success", "partial_success"} and not is_execution_error_result(result)

        if not success and error == "":
            error = str(result) if result is not None else "Action execution failed."

        normalized: ActionResult = {
            "action_call_id": str(record.get("action_call_id", "")),
            "ok": bool(record.get("ok", success)),
            "status": cast(Any, status),
            "purpose": purpose,
            "action_id": action_id,
            "tool_name": str(record.get("tool_name", action_id)),
            "kwargs": dict(kwargs),
            "todo_suggestion": next_step,
            "next": next_step,
            "success": success,
            "result": result,
            "data": record.get("data", result),
            "model_digest": record.get("model_digest", {}),
            "artifact_refs": record.get("artifact_refs", []),
            "artifacts": record.get("artifacts", []),
            "diagnostics": record.get(
                "diagnostics",
                result.get("diagnostics", []) if isinstance(result, dict) else [],
            ),
            "approval": record.get("approval", {}),
            "timing": record.get("timing", {}),
            "meta": record.get("meta", {}),
            "redaction_report": record.get("redaction_report", []),
            "error": error,
            "expose_to_model": bool(record.get("expose_to_model", True)),
            "side_effect_level": cast(Any, record.get("side_effect_level", "read")),
            "executor_type": str(record.get("executor_type", "")),
        }
        if not isinstance(normalized.get("model_digest"), dict) or not normalized.get("model_digest"):
            normalized.pop("model_digest", None)
        return normalized

    result = record
    success = not is_execution_error_result(result)
    return {
        "action_call_id": "",
        "ok": success,
        "status": "success" if success else "error",
        "purpose": fallback_purpose,
        "action_id": fallback_action_id,
        "tool_name": fallback_action_id,
        "kwargs": dict(fallback_kwargs),
        "todo_suggestion": fallback_next,
        "next": fallback_next,
        "success": success,
        "result": result,
        "data": result,
        "artifact_refs": [],
        "artifacts": [],
        "diagnostics": [],
        "approval": {},
        "timing": {},
        "meta": {},
        "redaction_report": [],
        "error": "" if success else str(result),
        "expose_to_model": True,
        "side_effect_level": "read",
        "executor_type": "",
    }


def should_continue(
    decision: ActionDecision,
    *,
    round_index: int,
    max_rounds: int | None,
) -> bool:
    if isinstance(max_rounds, int) and max_rounds >= 0 and round_index >= max_rounds:
        return False
    if decision.get("next_action") != "execute":
        return False
    if decision.get("use_action") is not True:
        return False
    commands = decision.get("action_calls")
    return isinstance(commands, list) and len(commands) > 0


def to_action_results(records: list[ActionResult]) -> dict[str, Any]:
    action_results: dict[str, Any] = {}
    used_keys: set[str] = set()

    for index, record in enumerate(records):
        purpose = record.get("purpose")
        if not isinstance(purpose, str) or purpose.strip() == "":
            purpose = f"action_call_{index + 1}"

        key = purpose
        suffix = 2
        while key in used_keys:
            key = f"{purpose} ({suffix})"
            suffix += 1

        used_keys.add(key)
        model_digest = record.get("model_digest")
        result_value = (
            model_digest
            if isinstance(model_digest, dict) and model_digest.get("same_as") != "result"
            else record.get("result", record.get("data"))
        )
        if record.get("success"):
            action_results[key] = result_value
        else:
            action_results[key] = {
                "error": record.get("error", "Action execution failed."),
                "result": result_value,
                "status": record.get("status", "error"),
            }

    return action_results
