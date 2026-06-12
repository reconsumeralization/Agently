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

from copy import deepcopy
from typing import Any, Callable, cast

from agently.types.data import ActionResult, ActionSpec


_REDACTED = "[REDACTED]"
_DEFAULT_VALIDATION_MARKERS = (
    "pytest",
    "pyright",
    "ruff",
    "mypy",
    "npm test",
    "pnpm test",
    "yarn test",
    "uv test",
)


def _redact_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _REDACTED for key in value.keys()}
    if isinstance(value, list):
        return [_REDACTED for _ in value]
    if value is None:
        return None
    return _REDACTED


def _sanitize_metadata_value(value: Any, *, parent_key: str = "") -> Any:
    if parent_key == "env":
        return _redact_env(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata_value(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_metadata_value(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_metadata_value(item, parent_key=parent_key) for item in value)
    return value


def sanitize_action_spec_for_metadata(spec: ActionSpec | dict[str, Any]) -> dict[str, Any]:
    """Return a model/host-visible copy of an action spec without raw env values."""

    return _sanitize_metadata_value(deepcopy(dict(spec)))


def _command_to_text(command: Any) -> str | None:
    if isinstance(command, str):
        return command
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return None


def _record_command(record: ActionResult | dict[str, Any]) -> str | None:
    kwargs = record.get("kwargs")
    if not isinstance(kwargs, dict):
        return None
    command = kwargs.get("cmd", kwargs.get("command"))
    return _command_to_text(command)


def _record_returncode(record: ActionResult | dict[str, Any]) -> int | None:
    for key in ("returncode", "return_code", "exit_code"):
        value = record.get(key)
        if isinstance(value, int):
            return value
    for payload_key in ("result", "data"):
        payload = record.get(payload_key)
        if not isinstance(payload, dict):
            continue
        for key in ("returncode", "return_code", "exit_code"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
    return None


def _is_validation_command(command: str, markers: list[str] | tuple[str, ...]) -> bool:
    lowered = command.lower()
    return any(marker.lower() in lowered for marker in markers)


def summarize_action_records(
    records: list[ActionResult] | list[dict[str, Any]] | None,
    *,
    validation_command_markers: list[str] | tuple[str, ...] | None = None,
    validation_command_predicate: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    safe_records = records if isinstance(records, list) else []
    markers = validation_command_markers or _DEFAULT_VALIDATION_MARKERS
    failed_actions: list[dict[str, Any]] = []
    commands_run: list[str] = []
    commands_attempted: list[str] = []
    latest_validation: dict[str, Any] | None = None

    for index, record in enumerate(safe_records):
        if not isinstance(record, dict):
            continue
        record_dict = cast(dict[str, Any], record)
        status = str(record_dict.get("status", ""))
        success = record_dict.get("success")
        if not isinstance(success, bool):
            success = status in {"success", "partial_success"}
        action_id = str(record_dict.get("action_id", record_dict.get("tool_name", "")))
        command = _record_command(record_dict)

        if not success:
            failed_actions.append(
                {
                    "index": index,
                    "action_id": action_id,
                    "status": status or "error",
                    "error": str(record_dict.get("error", "")),
                    "command": command,
                }
            )

        if command is not None:
            commands_attempted.append(command)
            if success:
                commands_run.append(command)

            is_validation = (
                validation_command_predicate(command, record_dict)
                if validation_command_predicate is not None
                else _is_validation_command(command, markers)
            )
            if is_validation:
                latest_validation = {
                    "index": index,
                    "action_id": action_id,
                    "command": command,
                    "status": "passed" if success else "failed",
                    "returncode": _record_returncode(record_dict),
                }

    return {
        "actions_attempted": len([record for record in safe_records if isinstance(record, dict)]),
        "failed_actions": failed_actions,
        "commands_run": commands_run,
        "commands_attempted": commands_attempted,
        "latest_validation": latest_validation,
        "validation_passed": (
            None if latest_validation is None else latest_validation.get("status") == "passed"
        ),
    }
