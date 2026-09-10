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

import hashlib
import inspect
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agently.builtins.plugins.PromptGenerator.modules.output_contract import (
    PYDANTIC_CONTRACT_META_KEY,
    output_schema_to_json_schema,
)
from agently.core.model import Prompt
from agently.core.orchestration import TriggerFlow
from agently.types.data import OutputValidateContext, StreamingData
from agently.utils import DataFormatter, DataLocator, DataPathBuilder, StreamingJSONParser

if TYPE_CHECKING:
    from .execution import AgentExecution
    from agently.core.model import ModelRequest
    from agently.types.data import OutputValidateHandler
    from .structured_string_continuation import StructuredStringContinuation


_TRUSTED_COMPLETION_SOURCES = {"observed_boundary", "final_reconciliation"}
_MAX_DOCUMENT_START_CONTEXT_CHARS = 1000
_MAX_CONTINUITY_CONTEXT_CHARS = 2000
_MAX_STRUCTURED_CONTINUITY_CONTEXT_CHARS = 80_000
_MAX_STATE_SUMMARY_CHARS = 2000
_NO_PROGRESS_LIMIT = 3


class LongOutputError(RuntimeError):
    """Raised when an enabled long-output invariant cannot be honored."""


class _FinalValidationError(LongOutputError):
    """A declared, model-repairable final acceptance failure."""


class _IncompleteContinuationHeader(Exception):
    """A provider-length segment that closed no complete trusted control header."""

    def __init__(
        self,
        *,
        observed_fields: list[str],
        missing_fields: list[str],
        observed_paths: list[str],
    ) -> None:
        self.observed_fields = observed_fields
        self.missing_fields = missing_fields
        self.observed_paths = observed_paths
        super().__init__(
            "Length-terminated continuation did not close required header "
            f"fields: {', '.join(missing_fields)}."
        )


class _ContinuationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path_key: str
    operation: Literal[
        "append_item",
        "append_text",
        "set_value",
        "declare_empty_list",
        "declare_empty_text",
    ]
    unit_index: int
    value: Any


class _ContinuationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int
    base_digest: str
    anchor: str
    updates: list[_ContinuationUpdate] = Field(default_factory=list)
    state_summary: str = ""
    completion: Literal["complete", "incomplete", "undetermined"]


@dataclass(frozen=True)
class _AssemblySlot:
    key: str
    path: tuple[str | int, ...]
    kind: Literal["list", "text", "value"]
    min_items: int | None = None
    max_items: int | None = None

    @property
    def display_path(self) -> str:
        if not self.path:
            return "$"
        parts: list[str] = []
        for part in self.path:
            if isinstance(part, int):
                parts.append(f"[{part}]")
            elif parts:
                parts.append(f".{part}")
            else:
                parts.append(part)
        return "".join(parts)


def _unwrap_output_declaration(
    value: Any,
) -> tuple[Any, dict[str, Any]]:
    if not isinstance(value, tuple) or not value:
        return value, {}
    value_tuple = cast(Sequence[Any], value)
    metadata = (
        value_tuple[3]
        if len(value_tuple) > 3 and isinstance(value_tuple[3], Mapping)
        else {}
    )
    contract = metadata.get(PYDANTIC_CONTRACT_META_KEY)
    constraints = (
        contract.get("constraints")
        if isinstance(contract, Mapping)
        else {}
    )
    return (
        value_tuple[0],
        dict(constraints) if isinstance(constraints, Mapping) else {},
    )


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        DataFormatter.sanitize(value, remain_type=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path_to_dot(path: tuple[str | int, ...]) -> str:
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif result:
            result += f".{part}"
        else:
            result = part
    return result


def _slot_key(index: int, path: tuple[str | int, ...]) -> str:
    label = str(path[-1]) if path else "$"
    return f"p{index}:{label[:48]}"


def _get_path(root: Any, path: tuple[str | int, ...], default: Any = None) -> Any:
    current = root
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return default
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
    return current


def _set_path(root: Any, path: tuple[str | int, ...], value: Any) -> Any:
    if not path:
        return value
    current = root
    for index, part in enumerate(path[:-1]):
        next_part = path[index + 1]
        if isinstance(part, int):
            if not isinstance(current, list):
                raise LongOutputError(f"Cannot assemble indexed path {_path_to_dot(path)}.")
            while len(current) <= part:
                current.append(None)
            if not isinstance(current[part], (dict, list)):
                current[part] = [] if isinstance(next_part, int) else {}
            current = current[part]
        else:
            if not isinstance(current, dict):
                raise LongOutputError(f"Cannot assemble mapping path {_path_to_dot(path)}.")
            if not isinstance(current.get(part), (dict, list)):
                current[part] = [] if isinstance(next_part, int) else {}
            current = current[part]
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(current, list):
            raise LongOutputError(f"Cannot assemble indexed path {_path_to_dot(path)}.")
        while len(current) <= last:
            current.append(None)
        current[last] = value
    else:
        if not isinstance(current, dict):
            raise LongOutputError(f"Cannot assemble mapping path {_path_to_dot(path)}.")
        current[last] = value
    return root


def _compile_slots(output_schema: Any) -> tuple[list[_AssemblySlot], Any]:
    slots: list[_AssemblySlot] = []

    def visit(value: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(value, list):
            slots.append(
                _AssemblySlot(
                    key=_slot_key(len(slots), path),
                    path=path,
                    kind="list",
                )
            )
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                visit(child, (*path, str(key)))
            return
        declared, constraints = _unwrap_output_declaration(value)
        if isinstance(declared, list):
            slots.append(
                _AssemblySlot(
                    key=_slot_key(len(slots), path),
                    path=path,
                    kind="list",
                    min_items=_nonnegative_int(constraints.get("minItems")),
                    max_items=_nonnegative_int(constraints.get("maxItems")),
                )
            )
            return
        kind: Literal["text", "value"] = "text" if declared is str else "value"
        slots.append(
            _AssemblySlot(
                key=_slot_key(len(slots), path),
                path=path,
                kind=kind,
            )
        )

    visit(output_schema, ())
    initial: Any = [] if isinstance(output_schema, list) else {}
    return slots, initial


def normalized_terminal(meta: Mapping[str, Any] | None) -> Literal["complete", "length", "unknown"]:
    terminal = dict(meta or {})
    finish_reason = str(terminal.get("finish_reason") or "").strip().lower()
    status = str(terminal.get("status") or "").strip().lower()
    incomplete_details = terminal.get("incomplete_details")
    incomplete_reason = (
        str(incomplete_details.get("reason") or "").strip().lower()
        if isinstance(incomplete_details, Mapping)
        else ""
    )
    complete_reasons = {"stop", "end_turn"}
    length_reasons = {"length", "max_tokens", "max_output_tokens"}
    complete_statuses = {"completed", "success", "succeeded"}
    # Explicit negative/unknown facts must not be masked by a positive sibling.
    supported = (
        finish_reason in {"", *complete_reasons, *length_reasons}
        and status in {"", "incomplete", *complete_statuses}
        and incomplete_reason in {"", "max_output_tokens"}
    )
    if supported:
        if finish_reason in length_reasons or incomplete_reason == "max_output_tokens":
            return "length"
        if status == "incomplete":
            if not finish_reason:
                return "unknown"
        elif finish_reason in complete_reasons or status in complete_statuses:
            return "complete"
        elif not finish_reason and not status:
            return "unknown"
    raise LongOutputError(
        "ensure_long_output requires an explicit normalized terminal fact; "
        f"received status={status or 'missing'}, finish_reason={finish_reason or 'missing'}, "
        f"incomplete_reason={incomplete_reason or 'missing'}."
    )


class LongOutputDelivery:
    """Private continuation, storage, replay, and validation owner."""

    def __init__(
        self,
        execution: "AgentExecution",
        *,
        ensure_keys: list[str] | None,
        ensure_all_keys: bool | None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
        key_style: Literal["dot", "slash"],
        max_retries: int,
        raise_ensure_failure: bool,
        request: "ModelRequest | None" = None,
    ) -> None:
        self.execution = execution
        self.request = request if request is not None else execution.request
        self.is_stage = request is not None
        self.storage_id = execution.id
        self.workspace = (
            execution.task_workspace._derive(execution_id=f"{execution.id}-long-output")
            if self.is_stage else execution.task_workspace
        )
        self.meta: dict[str, Any] = {}
        self.ensure_keys = ensure_keys
        self.ensure_all_keys = ensure_all_keys
        self.validate_handler = validate_handler
        self.key_style: Literal["dot", "slash"] = key_style
        self.max_retries = max_retries
        self.raise_ensure_failure = raise_ensure_failure
        current_prompt = self.request.prompt.get()
        self.prompt_snapshot = (
            dict(current_prompt)
            if isinstance(current_prompt, Mapping)
            else dict(execution.prompt_snapshot)
        )
        self.validation_prompt = Prompt(
            self.request.plugin_manager,
            self.request.settings,
            prompt_dict=self.prompt_snapshot,
            name=f"{execution.agent.name}-LongOutputValidationPrompt",
        )
        self.prompt_object = self.validation_prompt.to_prompt_object()
        self.output_format = str(getattr(self.prompt_object, "output_format", "") or "").lower()
        self.output_schema = getattr(self.prompt_object, "output", None)
        self.structured = bool(self.output_schema)
        self.slots: list[_AssemblySlot] = []
        self.slot_by_key: dict[str, _AssemblySlot] = {}
        self.slot_value_models: dict[str, type[BaseModel]] = {}
        self.value: Any = ""
        self.units: list[dict[str, Any]] = []
        self.revision = 0
        self.segment_index = 0
        self.no_progress_count = 0
        self.continuation_unit_count = 0
        self.state_summary = ""
        self.repair_feedback: dict[str, Any] | None = None
        self.validation_repair_count = 0
        self.final_result: Any = None
        self.final_result_object: BaseModel | None = None
        self.final_ref: dict[str, Any] | None = None
        self.current_manifest_ref: dict[str, Any] | None = None
        self.replayed_unit_count = 0
        self.request_count = 1
        self._field_continuation: StructuredStringContinuation | None = None

    def preflight(self) -> None:
        if self.structured:
            if self.output_format != "json":
                raise LongOutputError(
                    "ensure_long_output currently supports plain text and JSON structured output. "
                    f"Resolved output format '{self.output_format or 'unknown'}' is not losslessly assemblable."
                )
            self.slots, self.value = _compile_slots(self.output_schema)
            if not self.slots:
                raise LongOutputError(
                    "ensure_long_output could not compile any safe assembly slots from the output contract."
                )
            self.slot_by_key = {slot.key: slot for slot in self.slots}
            self.slot_value_models = {
                slot.key: self._build_slot_value_model(slot)
                for slot in self.slots
            }
        else:
            if self.output_format not in {"", "text", "markdown"}:
                raise LongOutputError(
                    "ensure_long_output requires a plain-text carrier or a declared JSON output contract."
                )
            self.value = ""

    def validate_complete_carrier(self, raw: str) -> None:
        """A complete terminal must not hide a repaired or discarded JSON tail."""
        if not self.structured:
            return
        try:
            evidence = StreamingJSONParser._inspect_json_prefix(raw, terminal_complete=True)
        except ValueError as error:
            raise LongOutputError("Expected one complete JSON carrier; malformed or trailing content was observed.") from error
        if not evidence.root_complete:
            raise LongOutputError("Expected one complete JSON carrier; the observed carrier is unfinished.")

    def needs_continuation(self, meta: Mapping[str, Any] | None, raw: str) -> bool:
        """Use provider facts first, then a real JSON boundary, never punctuation."""
        terminal = normalized_terminal(meta)
        if terminal == "complete":
            return False
        if self.structured:
            # This also rejects malformed prefixes and discarded trailing material.
            evidence = StreamingJSONParser._inspect_json_prefix(raw)
            if terminal == "unknown" and evidence.root_complete:
                return False
        return True

    def _build_slot_value_model(self, slot: _AssemblySlot) -> type[BaseModel]:
        declared = _get_path(self.output_schema, slot.path)
        if slot.kind == "list":
            declared, _constraints = _unwrap_output_declaration(declared)
            declared = declared[0] if isinstance(declared, list) and declared else Any
        prompt = Prompt(
            self.request.plugin_manager,
            self.request.settings,
            prompt_dict={
                "output": {"value": declared},
                "output_format": "json",
                "ensure_all_keys": True,
            },
            name=f"{self.execution.agent.name}-LongOutputSlot-{slot.key}",
        )
        return prompt.to_output_model(strict_output=True)

    def _validate_slot_value(self, slot: _AssemblySlot, value: Any) -> Any:
        output_model = self.slot_value_models.get(slot.key)
        if output_model is None:
            raise LongOutputError(f"Missing local output validator for path_key {slot.key}.")
        try:
            validated = output_model.model_validate({"value": value})
        except Exception as error:
            raise LongOutputError(
                f"Update value for {slot.key} does not satisfy its declared output schema: {error}"
            ) from error
        return validated.model_dump(mode="json")["value"]

    def _slot_value_contract(self, slot: _AssemblySlot) -> dict[str, Any]:
        declared = _get_path(self.output_schema, slot.path)
        if slot.kind == "list":
            declared, _constraints = _unwrap_output_declaration(declared)
            declared = (
                declared[0]
                if isinstance(declared, list) and declared
                else Any
            )
        return output_schema_to_json_schema(
            declared,
            strict_output=True,
        )

    @property
    def current_digest(self) -> str:
        if self._field_continuation is not None:
            return self._field_continuation.current_digest
        materialized = _canonical_json(self.value) if self.structured else str(self.value)
        return _sha256_text(materialized)

    @property
    def anchor(self) -> str:
        if not self.units:
            return ""
        return str(self.units[-1].get("digest") or "")

    async def _write_verified(self, relative_path: str, content: str) -> dict[str, Any]:
        if self.is_stage:
            relative_path = (self.workspace.fallback_root / relative_path).relative_to(self.workspace.root).as_posix()
        write_result = await self.workspace.write_file(
            relative_path,
            content,
            append=False,
        )
        size = len(content.encode("utf-8"))
        readback = await self.execution.task_workspace.read_file(
            write_result.path,
            max_bytes=max(1, size + 1),
        )
        if readback.truncated or readback.content != content:
            raise LongOutputError(f"TaskWorkspace readback mismatch for {relative_path}.")
        digest = _sha256_text(content)
        if readback.sha256 != digest or write_result.sha256 != digest:
            raise LongOutputError(f"TaskWorkspace digest mismatch for {relative_path}.")
        return {
            "path": readback.path,
            "bytes": readback.total_bytes,
            "sha256": readback.sha256,
        }

    async def _persist_raw_segment(self, text: str, *, response_id: str) -> dict[str, Any]:
        return await self._write_verified(
            f"long_output/{self.storage_id}/segments/{self.segment_index:06d}-{response_id}.txt",
            text,
        )

    async def _persist_unit(
        self,
        *,
        slot: _AssemblySlot | None,
        operation: str,
        index: int,
        value: Any,
        response_id: str,
        completion_source: str,
        unit_index: int | None = None,
        append_record: bool = True,
    ) -> dict[str, Any]:
        resolved_unit_index = len(self.units) if unit_index is None else unit_index
        serialized = (
            str(value)
            if operation in {"append_text", "declare_empty_text"}
            else _canonical_json(value)
        )
        digest = _sha256_text(serialized)
        unit_ref = await self._write_verified(
            (
                f"long_output/{self.storage_id}/units/"
                f"{resolved_unit_index:08d}-{digest[:16]}.json"
                if operation != "append_text"
                else
                f"long_output/{self.storage_id}/units/"
                f"{resolved_unit_index:08d}-{digest[:16]}.txt"
            ),
            serialized,
        )
        record = {
            "unit_index": resolved_unit_index,
            "revision": self.revision + 1,
            "segment_index": self.segment_index,
            "response_id": response_id,
            "path_key": slot.key if slot is not None else "$text",
            "path": slot.display_path if slot is not None else "$",
            "operation": operation,
            "index": index,
            "completion_source": completion_source,
            "digest": digest,
            "ref": unit_ref,
        }
        if append_record:
            self.units.append(record)
        return record

    async def _persist_manifest(self) -> None:
        self.revision += 1
        manifest = {
            "execution_id": self.execution.id,
            "revision": self.revision,
            "segment_index": self.segment_index,
            "digest": self.current_digest,
            "units": self.units,
        }
        manifest_text = _canonical_json(manifest)
        manifest_ref = await self._write_verified(
            f"long_output/{self.storage_id}/manifests/{self.revision:06d}.json",
            manifest_text,
        )
        self.current_manifest_ref = manifest_ref
        self.execution.diagnostics["long_output"] = {
            "enabled": True,
            "status": "assembling",
            "request_count": self.request_count,
            "segment_count": self.segment_index + 1,
            "manifest_revision": self.revision,
            "manifest_ref": manifest_ref,
            "accepted_unit_count": len(self.units),
            "current_digest": self.current_digest,
            "guarantee_level": "transport_and_schema",
        }

    async def _read_verified_ref(
        self,
        ref: Mapping[str, Any] | None,
        *,
        label: str,
    ) -> str:
        if not isinstance(ref, Mapping):
            raise LongOutputError(f"Missing TaskWorkspace ref for {label}.")
        path = ref.get("path")
        expected_bytes = ref.get("bytes")
        expected_digest = ref.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or not isinstance(expected_digest, str)
            or not expected_digest
        ):
            raise LongOutputError(f"Invalid TaskWorkspace ref for {label}.")
        readback = await self.execution.task_workspace.read_file(
            path,
            max_bytes=max(1, expected_bytes + 1),
        )
        actual_bytes = len(readback.content.encode("utf-8"))
        if (
            readback.truncated
            or readback.total_bytes != expected_bytes
            or actual_bytes != expected_bytes
            or readback.sha256 != expected_digest
            or _sha256_text(readback.content) != expected_digest
        ):
            raise LongOutputError(f"TaskWorkspace replay mismatch for {label}.")
        return readback.content

    async def _replay_latest_manifest(self) -> Any:
        if self._field_continuation is not None:
            return await self._field_continuation._replay_latest_manifest()
        manifest_text = await self._read_verified_ref(
            self.current_manifest_ref,
            label="latest long-output manifest",
        )
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError as error:
            raise LongOutputError("Latest long-output manifest is not valid JSON.") from error
        if not isinstance(manifest, Mapping):
            raise LongOutputError("Latest long-output manifest is not an object.")
        manifest_segment_index = manifest.get("segment_index")
        if (
            manifest.get("execution_id") != self.execution.id
            or manifest.get("revision") != self.revision
            or not isinstance(manifest_segment_index, int)
            or manifest_segment_index > self.segment_index
        ):
            raise LongOutputError("Latest long-output manifest lineage is stale.")
        records = manifest.get("units")
        if not isinstance(records, list) or len(records) != len(self.units):
            raise LongOutputError("Latest long-output manifest unit inventory does not match committed state.")

        candidate: Any = (
            [] if self.structured and isinstance(self.output_schema, list)
            else {} if self.structured
            else ""
        )
        slot_counts: dict[str, int] = {}
        for unit_position, raw_record in enumerate(records):
            if not isinstance(raw_record, Mapping):
                raise LongOutputError(f"Manifest unit {unit_position} is not an object.")
            if raw_record.get("unit_index") != unit_position:
                raise LongOutputError(f"Manifest unit {unit_position} is out of sequence.")
            if _canonical_json(raw_record) != _canonical_json(self.units[unit_position]):
                raise LongOutputError(f"Manifest unit {unit_position} does not match committed state.")
            operation = raw_record.get("operation")
            path_key = raw_record.get("path_key")
            index = raw_record.get("index")
            completion_source = raw_record.get("completion_source")
            if not isinstance(operation, str) or not isinstance(path_key, str) or not isinstance(index, int):
                raise LongOutputError(f"Manifest unit {unit_position} has an invalid operation header.")
            unit_text = await self._read_verified_ref(
                raw_record.get("ref") if isinstance(raw_record.get("ref"), Mapping) else None,
                label=f"long-output unit {unit_position}",
            )
            if _sha256_text(unit_text) != raw_record.get("digest"):
                raise LongOutputError(f"Manifest unit {unit_position} digest does not match its bytes.")

            if not self.structured:
                if (
                    operation != "append_text"
                    or path_key != "$text"
                    or index != unit_position
                    or completion_source not in {
                        "provider_text_prefix",
                        "observed_boundary",
                        "final_reconciliation",
                    }
                ):
                    raise LongOutputError(f"Plain-text manifest unit {unit_position} is not append-only.")
                candidate = str(candidate) + unit_text
                continue

            if completion_source not in _TRUSTED_COMPLETION_SOURCES:
                raise LongOutputError(
                    f"Structured manifest unit {unit_position} has untrusted completion provenance."
                )
            slot = self.slot_by_key.get(path_key)
            if slot is None or raw_record.get("path") != slot.display_path:
                raise LongOutputError(f"Manifest unit {unit_position} uses an unknown assembly slot.")
            committed_for_slot = slot_counts.get(slot.key, 0)
            if operation == "declare_empty_list":
                current = _get_path(candidate, slot.path)
                if (
                    slot.kind != "list"
                    or index != 0
                    or committed_for_slot
                    or unit_text != "[]"
                    or (current is not None and current != [])
                ):
                    raise LongOutputError(
                        f"Manifest unit {unit_position} has an invalid empty-list declaration."
                    )
                candidate = _set_path(candidate, slot.path, [])
                slot_counts[slot.key] = 1
                continue
            if operation == "declare_empty_text":
                current = _get_path(candidate, slot.path)
                if (
                    slot.kind != "text"
                    or index != 0
                    or committed_for_slot
                    or unit_text != ""
                    or (current is not None and current != "")
                ):
                    raise LongOutputError(
                        f"Manifest unit {unit_position} has an invalid empty-text declaration."
                    )
                candidate = _set_path(candidate, slot.path, "")
                slot_counts[slot.key] = 1
                continue
            expected_operation = (
                "append_item"
                if slot.kind == "list"
                else "append_text"
                if slot.kind == "text"
                else "set_value"
            )
            if operation != expected_operation:
                raise LongOutputError(f"Manifest unit {unit_position} uses a disallowed operation.")
            try:
                unit_value = unit_text if operation == "append_text" else json.loads(unit_text)
            except json.JSONDecodeError as error:
                raise LongOutputError(f"Manifest unit {unit_position} is not valid JSON.") from error

            if slot.kind == "list":
                current = _get_path(candidate, slot.path, [])
                expected_index = len(current) if isinstance(current, list) else 0
                if index != expected_index:
                    raise LongOutputError(f"Manifest list unit {unit_position} has a sequence gap.")
                if slot.max_items is not None and expected_index >= slot.max_items:
                    raise LongOutputError(
                        f"Manifest list unit {unit_position} exceeds the "
                        f"maximum item count {slot.max_items}."
                    )
                candidate = _set_path(
                    candidate,
                    slot.path,
                    [*list(current if isinstance(current, list) else []), unit_value],
                )
            elif slot.kind == "text":
                if index != committed_for_slot:
                    raise LongOutputError(f"Manifest text unit {unit_position} has a sequence gap.")
                current = _get_path(candidate, slot.path, "")
                candidate = _set_path(candidate, slot.path, str(current or "") + unit_text)
            else:
                if index != 0 or committed_for_slot:
                    raise LongOutputError(f"Manifest value unit {unit_position} is duplicated.")
                candidate = _set_path(candidate, slot.path, unit_value)
            slot_counts[slot.key] = committed_for_slot + 1

        replay_digest = _sha256_text(
            _canonical_json(candidate) if self.structured else str(candidate)
        )
        if replay_digest != manifest.get("digest") or replay_digest != self.current_digest:
            raise LongOutputError("Replayed long-output candidate digest does not match the manifest.")
        self.replayed_unit_count = len(records)
        return candidate

    @staticmethod
    def _event_is_trusted(event: StreamingData) -> bool:
        return bool(
            event.is_complete
            and event.completion_source in _TRUSTED_COMPLETION_SOURCES
        )

    async def accept_initial(
        self,
        result: Any,
        *,
        streaming_events: list[StreamingData],
    ) -> None:
        if self.is_stage:
            self.storage_id = f"{self.execution.id}/{result.id}"
        response_id = str(result.response_id or result.id)
        raw_text = await result.async_get_text()
        if self.structured:
            evidence = StreamingJSONParser._inspect_json_prefix(raw_text)
            if evidence.open_string_path is not None:
                from .structured_string_continuation import StructuredStringContinuation

                self._field_continuation = StructuredStringContinuation(self)
                await self._field_continuation.accept_initial(
                    result, streaming_events=streaming_events,
                )
                return
        await self._persist_raw_segment(raw_text, response_id=response_id)
        committed = 0
        if not self.structured:
            self.value = raw_text
            if raw_text:
                await self._persist_unit(
                    slot=None,
                    operation="append_text",
                    index=0,
                    value=raw_text,
                    response_id=response_id,
                    completion_source="provider_text_prefix",
                )
                committed = 1
        else:
            trusted = {
                event.path: event
                for event in streaming_events
                if self._event_is_trusted(event) and not event.path.startswith("$")
            }
            for slot in self.slots:
                slot_path = _path_to_dot(slot.path)
                if slot.kind == "list":
                    index = 0
                    while True:
                        item_path = f"{slot_path}[{index}]" if slot_path else f"[{index}]"
                        event = trusted.get(item_path)
                        if event is None:
                            break
                        if slot.max_items is not None and index >= slot.max_items:
                            self.execution.diagnostics.setdefault(
                                "long_output_rejected_updates",
                                [],
                            ).append(
                                {
                                    "segment_index": 0,
                                    "response_id": response_id,
                                    "accepted_prefix_count": index,
                                    "reason": (
                                        f"List slot {slot.key} reached maximum "
                                        f"item count {slot.max_items}."
                                    ),
                                }
                            )
                            break
                        try:
                            normalized_value = self._validate_slot_value(slot, event.value)
                        except LongOutputError as error:
                            self.execution.diagnostics.setdefault(
                                "long_output_rejected_updates",
                                [],
                            ).append(
                                {
                                    "segment_index": 0,
                                    "response_id": response_id,
                                    "accepted_prefix_count": index,
                                    "reason": str(error)[:500],
                                }
                            )
                            break
                        self.value = _set_path(
                            self.value,
                            slot.path,
                            [
                                *list(_get_path(self.value, slot.path, [])),
                                normalized_value,
                            ],
                        )
                        await self._persist_unit(
                            slot=slot,
                            operation="append_item",
                            index=index,
                            value=normalized_value,
                            response_id=response_id,
                            completion_source=str(event.completion_source),
                        )
                        committed += 1
                        index += 1
                    list_event = trusted.get(slot_path)
                    if (
                        index == 0
                        and list_event is not None
                        and list_event.value == []
                    ):
                        if slot.min_items is not None and slot.min_items > 0:
                            self.execution.diagnostics.setdefault(
                                "long_output_rejected_updates",
                                [],
                            ).append(
                                {
                                    "segment_index": 0,
                                    "response_id": response_id,
                                    "accepted_prefix_count": 0,
                                    "reason": (
                                        f"List slot {slot.key} requires at least "
                                        f"{slot.min_items} items and cannot be empty."
                                    ),
                                }
                            )
                            continue
                        self.value = _set_path(
                            self.value,
                            slot.path,
                            [],
                        )
                        await self._persist_unit(
                            slot=slot,
                            operation="declare_empty_list",
                            index=0,
                            value=[],
                            response_id=response_id,
                            completion_source=str(
                                list_event.completion_source
                            ),
                        )
                        committed += 1
                else:
                    event = trusted.get(slot_path)
                    if event is None:
                        continue
                    try:
                        normalized_value = self._validate_slot_value(slot, event.value)
                    except LongOutputError as error:
                        self.execution.diagnostics.setdefault(
                            "long_output_rejected_updates",
                            [],
                        ).append(
                            {
                                "segment_index": 0,
                                "response_id": response_id,
                                "accepted_prefix_count": 0,
                                "reason": str(error)[:500],
                            }
                        )
                        continue
                    self.value = _set_path(
                        self.value,
                        slot.path,
                        normalized_value,
                    )
                    await self._persist_unit(
                        slot=slot,
                        operation=(
                            (
                                "declare_empty_text"
                                if normalized_value == ""
                                else "append_text"
                            )
                            if slot.kind == "text"
                            else "set_value"
                        ),
                        index=0,
                        value=normalized_value,
                        response_id=response_id,
                        completion_source=str(event.completion_source),
                    )
                    committed += 1
        await self._persist_manifest()
        await self.execution.emit_stream(
            "long_output.initial_committed",
            {
                "response_id": response_id,
                "accepted_unit_count": committed,
                "manifest_revision": self.revision,
                "digest": self.current_digest,
            },
            route="model_request",
            source="long_output",
            meta={"stream_kind": "status", "status": "continuing"},
        )

    def _slot_state(self) -> list[dict[str, Any]]:
        state: list[dict[str, Any]] = []
        for slot in self.slots:
            current = _get_path(self.value, slot.path)
            committed_for_slot = [
                unit for unit in self.units if unit.get("path_key") == slot.key
            ]
            if slot.kind in {"text", "value"} and committed_for_slot:
                continue
            if slot.kind == "list":
                next_index = len(current) if isinstance(current, list) else 0
                if slot.max_items is not None and next_index >= slot.max_items:
                    continue
            elif slot.kind == "text":
                next_index = len(committed_for_slot)
            else:
                next_index = 1 if committed_for_slot else 0
            declaration = _get_path(self.output_schema, slot.path)
            state.append(
                {
                    "path_key": slot.key,
                    **(
                        {"description": str(declaration[1])}
                        if slot.kind == "list"
                        and isinstance(declaration, tuple)
                        and len(declaration) > 1
                        and declaration[1]
                        else {}
                    ),
                    "operation": (
                        "append_item"
                        if slot.kind == "list"
                        else "append_text"
                        if slot.kind == "text"
                        else "set_value"
                    ),
                    "next_unit_index": next_index,
                    "is_set": bool(committed_for_slot),
                    "value_contract": self._slot_value_contract(slot),
                    **(
                        {
                            **(
                                {"min_items": slot.min_items}
                                if slot.min_items is not None
                                else {}
                            ),
                            **(
                                {"max_items": slot.max_items}
                                if slot.max_items is not None
                                else {}
                            ),
                        }
                        if slot.kind == "list"
                        else {}
                    ),
                    **(
                        {
                            "empty_operation": "declare_empty_list",
                            "empty_is_declared": any(
                                unit.get("operation")
                                == "declare_empty_list"
                                for unit in committed_for_slot
                            ),
                        }
                        if slot.kind == "list"
                        else {
                            "empty_operation": "declare_empty_text",
                            "empty_is_declared": any(
                                unit.get("operation")
                                == "declare_empty_text"
                                for unit in committed_for_slot
                            ),
                        }
                        if slot.kind == "text"
                        else {}
                    ),
                }
            )
            if slot.kind == "list" and slot.max_items is not None:
                break
        return state

    def _structured_continuity_context(self) -> dict[str, Any]:
        serialized = _canonical_json(self.value)
        character_count = len(serialized)
        if character_count <= _MAX_STRUCTURED_CONTINUITY_CONTEXT_CHARS:
            return {
                "accepted_json": serialized,
                "accepted_serialized_character_count": character_count,
                "complete_snapshot": True,
            }
        excerpt_size = _MAX_STRUCTURED_CONTINUITY_CONTEXT_CHARS // 2
        return {
            "accepted_head": serialized[:excerpt_size],
            "accepted_tail": serialized[-excerpt_size:],
            "accepted_serialized_character_count": character_count,
            "complete_snapshot": False,
        }

    def _continuation_input(self) -> dict[str, Any]:
        return {
            "base_revision": self.revision,
            "base_digest": self.current_digest,
            "anchor": self.anchor,
            **(
                {
                    "continuity_context": (
                        self._structured_continuity_context()
                    ),
                }
                if self.structured
                else
                {
                    "continuity_context": {
                        "document_start": str(self.value)[
                            :_MAX_DOCUMENT_START_CONTEXT_CHARS
                        ],
                        "accepted_tail": str(self.value)[
                            -_MAX_CONTINUITY_CONTEXT_CHARS:
                        ],
                        "accepted_character_count": len(str(self.value)),
                    },
                }
            ),
            "assembly_slots": self._slot_state() if self.structured else [
                {
                    "path_key": "$text",
                    "operation": "append_text",
                    "next_unit_index": len(self.units),
                    "is_set": bool(self.value),
                    "value_contract": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000,
                    },
                }
            ],
            "accepted_unit_count": len(self.units),
            "state_summary": self.state_summary,
            "repair_feedback": self.repair_feedback,
        }

    def _build_continuation_request(self):
        request = self.execution.agent.create_request(
            name=f"{self.execution.agent.name}-LongOutputContinuation",
            inherit_agent_prompt=False,
            inherit_extension_handlers=False,
            model_key=getattr(self.request, "_model_key", None),
        )
        local_settings = self.request.settings.get(inherit=False)
        if isinstance(local_settings, dict):
            request.settings.update(deepcopy(local_settings))
        original_instruct = deepcopy(self.prompt_snapshot.get("instruct"))
        request.prompt.update(self.prompt_snapshot)
        request.prompt.set("tools", None)
        request.prompt.set("action_results", None)
        continuation_input = {
            "original_input": self.prompt_snapshot.get("input"),
            "long_output_continuation": self._continuation_input(),
        }
        # JSON preserves the exact text join without YAML presentation indentation.
        # This changes only the private carrier, not its values or accepted bytes.
        request.prompt.set("input", {
            "long_output_continuation": continuation_input["long_output_continuation"],
            "original_input": continuation_input["original_input"],
        } if self.structured else json.dumps(continuation_input, ensure_ascii=False))
        request.prompt.append("info", {"original_deliverable_instructions": original_instruct})
        protocol = [
            "Inspect the accepted ending before adding text. A missing terminal signal does not prove missing content; if this deliverable is already complete, confirm completion with no updates.",
            "Continue only the current request deliverable defined by [input.original_input] and [info.original_deliverable_instructions]. Surrounding task or document plans are context, not additional output.",
            "Even when the original request asks for a complete rewrite, the accepted prefix is already part of that rewrite. Append its missing suffix; do not execute the original request again.",
            "Input references in the original instructions refer to [input.original_input]; original output references describe business values, not this private envelope.",
            "Return only the private envelope in [output], with business content inside [output.updates]. Start with base_revision, base_digest, then anchor, copying them exactly from [input.long_output_continuation] before any updates.",
            "Use only offered assembly_slots: copy the complete path_key and allowed operation. Each value must satisfy its value_contract and any slot description. unit_index is the zero-based assembly position, not a field inside the business value.",
            "Never repeat or modify accepted units. Repeated content genuinely required within a new unit remains allowed.",
        ]
        if self.structured:
            protocol.extend([
                "For structured output, continuity_context is read-only accepted business evidence, not a response shape or update target. Preserve its language, naming and cross-field facts. complete_snapshot=false means the middle is omitted; do not invent facts about it.",
                "Finish offered slots in schema order; later slots may be withheld behind an incomplete exact-count list. Never address a slot that is not offered.",
                "For append_item, emit complete items from next_unit_index onward. Preserve nested JSON kinds and wrappers exactly. Honor min_items/max_items as total list bounds, not the size of this response.",
                "For an intentionally empty offered list or text slot, use its empty_operation with unit_index=0 and value=[] or value=\"\" respectively. Never repeat an empty declaration or use it after accepted content.",
                "An offered append_text slot is an unset atomic JSON string: one complete update closes it. Committed strings are immutable and not offered again. Emit at most 4000 characters; a larger logical value needs an original chunk-list contract.",
                "For set_value, supply the missing value with unit_index=0. Emit at most four updates per envelope, leaving room to close it.",
            ])
        else:
            protocol.extend([
                "For plain text, continuity_context.accepted_tail is the exact join; document_start supplies formatting conventions. Finish the open word, sentence, item or construct first. Add only whitespace needed by the text, not transport indentation. accepted_character_count is Host-measured.",
                "Emit one append_text update using next_unit_index, or none if nothing remains. There is no minimum block length. Complete the remaining deliverable in this update when it fits value_contract and the response window; otherwise close a useful block and leave room to close the envelope. Do not add filler, another conclusion, or later workflow content merely to continue.",
            ])
        protocol.extend([
            "Apply repair_feedback when present. After an incomplete envelope or rejected update, use one smaller corrected update that can close; preserve every accepted unit.",
            "Set completion=complete when the current request deliverable is complete after these updates, even if the surrounding task has later stages; otherwise use incomplete. If the supplied context is insufficient to decide or continue safely, use undetermined rather than guessing or adding filler.",
            "Keep state_summary under 2000 characters: only next-use continuity facts, or the concrete evidence gap for undetermined. Leave it empty when none are needed.",
        ])
        request.prompt.set("instruct", {"long_output_delivery_protocol": protocol})
        request.output(_ContinuationEnvelope, format="json")
        return request

    @staticmethod
    def _trusted_event_map(events: list[StreamingData]) -> dict[str, StreamingData]:
        return {
            event.path: event
            for event in events
            if event.is_complete
            and event.completion_source in _TRUSTED_COMPLETION_SOURCES
            and not event.path.startswith("$")
        }

    def _envelope_from_length_events(
        self,
        events: list[StreamingData],
    ) -> tuple[int, str, str, list[_ContinuationUpdate], str, bool]:
        trusted = self._trusted_event_map(events)
        header_fields = ["base_revision", "base_digest", "anchor"]
        observed_fields = [
            field_name for field_name in header_fields if field_name in trusted
        ]
        missing_fields = [
            field_name
            for field_name in header_fields
            if field_name not in trusted
        ]
        if missing_fields:
            raise _IncompleteContinuationHeader(
                observed_fields=observed_fields,
                missing_fields=missing_fields,
                observed_paths=list(trusted)[:20],
            )
        try:
            base_revision = int(trusted["base_revision"].value)
            base_digest = str(trusted["base_digest"].value)
            anchor = str(trusted["anchor"].value)
        except (TypeError, ValueError) as error:
            raise LongOutputError(
                "Length-terminated continuation closed an invalid revision header."
            ) from error
        indexed_updates: list[tuple[int, _ContinuationUpdate]] = []
        for path, event in trusted.items():
            match = re.fullmatch(r"updates\[(\d+)\]", path)
            if match is None:
                continue
            indexed_updates.append(
                (int(match.group(1)), _ContinuationUpdate.model_validate(event.value))
            )
        indexed_updates.sort(key=lambda item: item[0])
        updates = [update for _index, update in indexed_updates]
        state_summary_event = trusted.get("state_summary")
        state_summary = (
            str(state_summary_event.value)[:_MAX_STATE_SUMMARY_CHARS]
            if state_summary_event is not None
            else self.state_summary
        )
        return base_revision, base_digest, anchor, updates, state_summary, False

    async def _record_no_progress(
        self,
        *,
        response_id: str,
        provider_terminal: Literal["complete", "length", "unknown"],
        reason_code: str,
        reason: str,
        action: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.no_progress_count += 1
        feedback = {
            "previous_segment_index": self.segment_index,
            "accepted_prefix_count": 0,
            "reason_code": reason_code,
            "recovery_attempt": self.no_progress_count,
            "action": action,
            "reason": reason[:500],
        }
        if details:
            feedback.update(DataFormatter.sanitize(dict(details)))
        self.repair_feedback = feedback
        record = {
            "segment_index": self.segment_index,
            "response_id": response_id,
            "reason_code": reason_code,
            "reason": reason[:500],
            **DataFormatter.sanitize(dict(details or {})),
            "manifest_revision": self.revision,
            "manifest_digest": self.current_digest,
            "accepted_unit_count": len(self.units),
            "no_progress_count": self.no_progress_count,
        }
        diagnostics = self.execution.diagnostics.setdefault(
            "long_output_no_progress",
            [],
        )
        if isinstance(diagnostics, list):
            diagnostics.append(record)
        long_output_diagnostics = dict(
            self.execution.diagnostics.get("long_output", {})
        )
        long_output_diagnostics.update(
            {
                "enabled": True,
                "status": "assembling",
                "request_count": self.request_count,
                "segment_count": self.segment_index + 1,
                "manifest_revision": self.revision,
                "manifest_ref": self.current_manifest_ref,
                "accepted_unit_count": len(self.units),
                "current_digest": self.current_digest,
                "no_progress_count": self.no_progress_count,
                "last_no_progress": record,
                "guarantee_level": "transport_and_schema",
            }
        )
        self.execution.diagnostics["long_output"] = long_output_diagnostics
        await self.execution.emit_stream(
            "long_output.segment_no_progress",
            record,
            route="model_request",
            source="long_output",
            meta={
                "stream_kind": "status",
                "status": "continuing",
                "provider_terminal": provider_terminal,
            },
        )

    async def _consume_continuation_result(
        self,
        result: Any,
    ) -> tuple[
        Literal["complete", "length", "unknown"],
        list[StreamingData],
        _ContinuationEnvelope | None,
        str | None,
    ]:
        events: list[StreamingData] = []
        async for event in result.get_async_generator(type="instant"):
            events.append(event)
        meta = await result.async_get_meta()
        terminal = normalized_terminal(meta)
        raw = await result.async_get_text()
        try:
            evidence = StreamingJSONParser._inspect_json_prefix(raw)
        except ValueError as error:
            return terminal, events, None, str(error)[:500]
        if terminal == "complete" or evidence.root_complete:
            try:
                evidence = StreamingJSONParser._inspect_json_prefix(
                    raw, terminal_complete=True,
                )
                if not evidence.root_complete:
                    raise ValueError("The private continuation envelope is unfinished.")
                parsed = await result.async_get_data(
                    type="parsed",
                    ensure_keys=[
                        "base_revision",
                        "base_digest",
                        "anchor",
                        "updates",
                        "state_summary",
                        "completion",
                    ],
                    max_retries=0,
                    raise_ensure_failure=False,
                )
                envelope = _ContinuationEnvelope.model_validate(parsed)
            except (TypeError, ValueError, ValidationError) as error:
                return terminal, events, None, str(error)[:500]
            return terminal, events, envelope, None
        return terminal, events, None, None

    def _validate_update(
        self,
        update: _ContinuationUpdate,
        *,
        candidate: Any,
        slot_counts: Mapping[str, int],
        total_unit_count: int,
    ) -> tuple[_AssemblySlot | None, _ContinuationUpdate]:
        if not self.structured:
            if update.path_key != "$text" or update.operation != "append_text":
                raise LongOutputError("Plain-text continuation used an unauthorized assembly slot.")
            if update.unit_index != total_unit_count:
                raise LongOutputError("Plain-text continuation block index is stale or non-contiguous.")
            if not isinstance(update.value, str) or update.value == "":
                raise LongOutputError("Plain-text continuation block must be a non-empty string.")
            if len(update.value) > 4000:
                raise LongOutputError("Plain-text continuation block exceeds the 4000-character safety boundary.")
            return None, update
        slot = self.slot_by_key.get(update.path_key)
        if slot is None:
            raise LongOutputError(f"Unknown long-output path_key: {update.path_key}.")
        committed_for_slot = int(slot_counts.get(slot.key, 0))
        current = _get_path(candidate, slot.path)
        if update.operation == "declare_empty_list":
            if slot.kind != "list":
                raise LongOutputError(
                    "declare_empty_list is allowed only for a list slot."
                )
            if slot.min_items is not None and slot.min_items > 0:
                raise LongOutputError(
                    f"List slot {slot.key} requires at least "
                    f"{slot.min_items} items and cannot be empty."
                )
            if (
                update.unit_index != 0
                or committed_for_slot
                or update.value != []
                or (current is not None and current != [])
            ):
                raise LongOutputError(
                    f"Empty-list declaration for {slot.key} is stale or invalid."
                )
            return slot, update.model_copy(update={"value": []})
        if update.operation == "declare_empty_text":
            if slot.kind != "text":
                raise LongOutputError(
                    "declare_empty_text is allowed only for a text slot."
                )
            if (
                update.unit_index != 0
                or committed_for_slot
                or update.value != ""
                or (current is not None and current != "")
            ):
                raise LongOutputError(
                    f"Empty-text declaration for {slot.key} is stale or invalid."
                )
            return slot, update.model_copy(update={"value": ""})
        expected_operation = (
            "append_item"
            if slot.kind == "list"
            else "append_text"
            if slot.kind == "text"
            else "set_value"
        )
        if update.operation != expected_operation:
            raise LongOutputError(
                f"Operation {update.operation} is not allowed for path_key {slot.key}."
            )
        if slot.kind == "list":
            expected_index = len(current) if isinstance(current, list) else 0
            if slot.max_items is not None and expected_index >= slot.max_items:
                raise LongOutputError(
                    f"List slot {slot.key} reached maximum "
                    f"item count {slot.max_items}."
                )
        elif slot.kind == "text":
            if committed_for_slot:
                raise LongOutputError(
                    f"Text slot {slot.key} is already committed."
                )
            expected_index = committed_for_slot
            if not isinstance(update.value, str) or update.value == "":
                raise LongOutputError(f"Text update for {slot.key} must be a non-empty string.")
            if len(update.value) > 4000:
                raise LongOutputError(f"Text update for {slot.key} exceeds 4000 characters.")
        else:
            expected_index = 0
            if committed_for_slot:
                raise LongOutputError(f"Value slot {slot.key} is already committed.")
        if update.unit_index != expected_index:
            raise LongOutputError(
                "Update unit_index for "
                f"{slot.key} is stale: expected {expected_index}, got {update.unit_index}."
            )
        normalized_value = self._validate_slot_value(slot, update.value)
        return slot, update.model_copy(update={"value": normalized_value})

    def _apply_candidate_update(
        self,
        candidate: Any,
        update: _ContinuationUpdate,
        slot: _AssemblySlot | None,
    ) -> Any:
        if not self.structured:
            return str(candidate) + str(update.value)
        elif (
            slot is not None
            and update.operation == "declare_empty_list"
        ):
            return _set_path(candidate, slot.path, [])
        elif (
            slot is not None
            and update.operation == "declare_empty_text"
        ):
            return _set_path(candidate, slot.path, "")
        elif slot is not None and slot.kind == "list":
            current = _get_path(candidate, slot.path, [])
            return _set_path(
                candidate,
                slot.path,
                [*list(current if isinstance(current, list) else []), DataFormatter.sanitize(update.value)],
            )
        elif slot is not None and slot.kind == "text":
            current = _get_path(candidate, slot.path, "")
            return _set_path(
                candidate,
                slot.path,
                str(current if isinstance(current, str) else "") + str(update.value),
            )
        elif slot is not None:
            return _set_path(
                candidate,
                slot.path,
                DataFormatter.sanitize(update.value),
            )
        return candidate

    def _prepare_valid_update_prefix(
        self,
        updates: list[_ContinuationUpdate],
    ) -> tuple[list[tuple[_ContinuationUpdate, _AssemblySlot | None]], Any, str | None]:
        candidate = deepcopy(self.value)
        slot_counts: dict[str, int] = {}
        for unit in self.units:
            path_key = str(unit.get("path_key") or "")
            slot_counts[path_key] = slot_counts.get(path_key, 0) + 1
        prepared: list[tuple[_ContinuationUpdate, _AssemblySlot | None]] = []
        rejected_reason: str | None = None
        for update in updates:
            if not self.structured and prepared:
                rejected_reason = (
                    "Plain-text continuation permits exactly one append_text update "
                    "per logical request."
                )
                break
            try:
                slot, normalized_update = self._validate_update(
                    update,
                    candidate=candidate,
                    slot_counts=slot_counts,
                    total_unit_count=len(self.units) + len(prepared),
                )
            except LongOutputError as error:
                rejected_reason = str(error)
                break
            candidate = self._apply_candidate_update(candidate, normalized_update, slot)
            path_key = normalized_update.path_key
            slot_counts[path_key] = slot_counts.get(path_key, 0) + 1
            prepared.append((normalized_update, slot))
        return prepared, candidate, rejected_reason

    async def request_and_commit_next(self) -> dict[str, Any]:
        if self._field_continuation is not None:
            return await self._field_continuation.request_and_commit_next()
        request = self._build_continuation_request()
        result = request.get_result(
            parent_run_context=self.execution.agent_execution_run_context,
        )
        self.request_count += 1
        self.execution.record_model_response_id(result.id)
        (
            terminal,
            events,
            envelope,
            envelope_error,
        ) = await self._consume_continuation_result(result)
        self.segment_index += 1
        response_id = str(result.response_id or result.id)
        raw_text = await result.async_get_text()
        await self._persist_raw_segment(raw_text, response_id=response_id)
        if envelope_error is not None:
            reason = (
                "Provider-complete continuation did not satisfy the private "
                f"envelope contract: {envelope_error or 'validation failed'}"
            )
            await self._record_no_progress(
                response_id=response_id,
                provider_terminal=terminal,
                reason_code="continuation_envelope_invalid",
                reason=reason,
                action=(
                    "Return only the declared continuation envelope. Start "
                    "with the exact base_revision, base_digest, and anchor, "
                    "then emit at most one corrected complete update."
                ),
            )
            return {
                "progress": False,
                "is_final": False,
                "terminal": terminal,
            }
        try:
            if envelope is not None:
                assert isinstance(envelope, _ContinuationEnvelope)
                base_revision = envelope.base_revision
                base_digest = envelope.base_digest
                anchor = envelope.anchor
                updates = envelope.updates
                state_summary = envelope.state_summary[:_MAX_STATE_SUMMARY_CHARS]
                is_final = envelope.completion == "complete"
                completion_source = "final_reconciliation"
            else:
                (
                    base_revision,
                    base_digest,
                    anchor,
                    updates,
                    state_summary,
                    is_final,
                ) = self._envelope_from_length_events(events)
                completion_source = "observed_boundary"
        except _IncompleteContinuationHeader as error:
            reason = str(error)
            await self._record_no_progress(
                response_id=response_id,
                provider_terminal=terminal,
                reason_code="continuation_header_incomplete",
                reason=reason,
                action=(
                    "Start the next response immediately with base_revision, "
                    "base_digest, and anchor. Close all three fields before "
                    "emitting at most one update."
                ),
                details={
                    "observed_header_fields": error.observed_fields,
                    "missing_header_fields": error.missing_fields,
                    "observed_complete_paths": error.observed_paths,
                },
            )
            return {
                "progress": False,
                "is_final": False,
                "terminal": terminal,
            }
        if (
            base_revision != self.revision
            or base_digest != self.current_digest
            or anchor != self.anchor
        ):
            raise LongOutputError(
                "Continuation response did not match the current manifest revision, digest, and anchor."
            )
        prepared, candidate, rejected_reason = self._prepare_valid_update_prefix(updates)
        if envelope is not None and envelope.completion == "undetermined":
            raise LongOutputError(
                "Continuation completion is undetermined: "
                + (state_summary or "The supplied context does not establish safe completion.")
            )
        new_records: list[dict[str, Any]] = []
        for update, slot in prepared:
            new_records.append(
                await self._persist_unit(
                    slot=slot,
                    operation=update.operation,
                    index=update.unit_index,
                    value=update.value,
                    response_id=response_id,
                    completion_source=completion_source,
                    unit_index=len(self.units) + len(new_records),
                    append_record=False,
                )
            )
        self.units.extend(new_records)
        if new_records:
            self.value = candidate
        committed_updates = [update for update, _slot in prepared]
        committed = len(new_records)
        missing_required_paths = (
            self._missing_ensure_paths(self.value)
            if self.structured and is_final and rejected_reason is None
            else []
        )
        if missing_required_paths:
            is_final = False
        if rejected_reason is not None:
            is_final = False
            self.repair_feedback = {
                "previous_segment_index": self.segment_index,
                "accepted_prefix_count": committed,
                "reason_code": "continuation_update_rejected",
                "action": (
                    "Regenerate from the offered next_unit_index. Correct the first "
                    "rejected update before emitting later updates."
                ),
                "reason": rejected_reason[:500],
            }
            rejected = self.execution.diagnostics.setdefault(
                "long_output_rejected_updates",
                [],
            )
            if isinstance(rejected, list):
                rejected.append(
                    {
                        "segment_index": self.segment_index,
                        "response_id": response_id,
                        "accepted_prefix_count": committed,
                        "reason": rejected_reason[:500],
                    }
                )
        elif missing_required_paths:
            self.repair_feedback = {
                "previous_segment_index": self.segment_index,
                "accepted_prefix_count": committed,
                "reason_code": "continuation_required_slots_missing",
                "missing_paths": missing_required_paths[:20],
                "action": (
                    "The manifest is not complete. Emit updates for the missing "
                    "required paths only; use the offered empty declaration when "
                    "an intentionally empty list or text field is required."
                ),
                "reason": (
                    "Required manifest paths are missing: "
                    + ", ".join(missing_required_paths[:20])
                )[:500],
            }
        elif committed or is_final:
            self.repair_feedback = None
        else:
            self.repair_feedback = {
                "previous_segment_index": self.segment_index,
                "accepted_prefix_count": 0,
                "action": (
                    "The previous response ended before one complete valid update. "
                    "Emit a smaller envelope and close the first update."
                ),
                "reason": "no complete valid update was observed",
            }
        self.continuation_unit_count += committed
        self.state_summary = state_summary
        if committed:
            self.no_progress_count = 0
            await self._persist_manifest()
            for update in committed_updates:
                if update.operation == "append_text":
                    await self.execution.emit_stream(
                        "model.text",
                        update.value,
                        route="model_request",
                        source="long_output",
                        delta=str(update.value),
                        event_type="delta",
                        is_complete=True,
                        meta={
                            "stream_kind": "text",
                            "manifest_revision": self.revision,
                            "committed": True,
                        },
                    )
            await self.execution.emit_stream(
                "long_output.segment_committed",
                {
                    "response_id": response_id,
                    "accepted_unit_count": committed,
                    "rejected_tail": rejected_reason is not None,
                    "manifest_revision": self.revision,
                    "digest": self.current_digest,
                    "provider_terminal": terminal,
                },
                route="model_request",
                source="long_output",
                meta={"stream_kind": "status", "status": "continuing"},
            )
        elif is_final:
            # A fully observed, correlated final acknowledgement is progress
            # even when no new business text is needed. Original replay and
            # validation still decide whether the candidate may be delivered.
            self.no_progress_count = 0
        else:
            if rejected_reason is not None:
                reason_code = "continuation_update_rejected"
                no_progress_reason = rejected_reason
                no_progress_action = (
                    (
                        "Do not repeat the empty declaration. The slot is "
                        "already complete; continue with another missing slot."
                    )
                    if rejected_reason.startswith(
                        ("Empty-list declaration", "Empty-text declaration")
                    )
                    else (
                        "Regenerate the first rejected update from the offered "
                        "next_unit_index before emitting later updates."
                    )
                )
            elif missing_required_paths:
                reason_code = "continuation_required_slots_missing"
                no_progress_reason = (
                    "Required manifest paths are missing: "
                    + ", ".join(missing_required_paths[:20])
                )
                no_progress_action = (
                    "Emit updates for the missing required paths only; use the "
                    "offered empty declaration for intentionally empty list or "
                    "text fields."
                )
            else:
                reason_code = "continuation_no_complete_update"
                no_progress_reason = "no complete valid update was observed"
                no_progress_action = (
                    "Emit a smaller envelope and close the first update before "
                    "adding another update."
                )
            await self._record_no_progress(
                response_id=response_id,
                provider_terminal=terminal,
                reason_code=reason_code,
                reason=no_progress_reason,
                action=no_progress_action,
                details=(
                    {"missing_paths": missing_required_paths[:20]}
                    if missing_required_paths
                    else None
                ),
            )
        return {
            "progress": committed > 0 or bool(is_final),
            "is_final": bool(
                is_final
                and rejected_reason is None
                and envelope is not None
            ),
            "terminal": terminal,
        }

    async def _run_custom_validators(self, candidate: Any, result_object: BaseModel | None) -> None:
        handlers = self.request.extension_handlers.get("validate_handlers", [])
        resolved = list(handlers) if isinstance(handlers, list) else []
        if self.validate_handler is not None:
            if isinstance(self.validate_handler, list):
                resolved.extend(self.validate_handler)
            else:
                resolved.append(self.validate_handler)
        if not resolved:
            return
        validate_value = dict(candidate) if isinstance(candidate, Mapping) else {"value": candidate}
        response_text = str(candidate) if isinstance(candidate, str) else _canonical_json(candidate)
        for index, handler in enumerate(resolved):
            validator_name = getattr(handler, "__name__", None) or f"validate_handler_{index + 1}"
            context = OutputValidateContext(
                value=validate_value,
                agent_name=self.execution.agent.name,
                response_id=str(getattr(self.execution._model_request_result, "response_id", "")),
                attempt_index=1,
                retry_count=0,
                max_retries=self.max_retries,
                prompt=self.validation_prompt,
                settings=self.request.settings,
                request_run_context=getattr(
                    self.execution._model_request_result,
                    "request_run_context",
                    None,
                ),
                model_run_context=getattr(
                    self.execution._model_request_result,
                    "model_run_context",
                    None,
                ),
                response_text=response_text,
                parsed_result=candidate,
                result_object=result_object,
                meta={"validator_name": validator_name, "long_output": True},
            )
            outcome = handler(validate_value, context)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if isinstance(outcome, bool):
                ok = outcome
                reason = f"Validation failed in {validator_name}."
                explicit_error = None
            elif isinstance(outcome, Mapping):
                ok = bool(outcome.get("ok", False))
                reason = str(outcome.get("reason") or f"Validation failed in {validator_name}.")
                explicit_error = outcome.get(
                    "raise",
                    outcome.get("error", outcome.get("exception")),
                )
            else:
                raise LongOutputError(
                    f"Unsupported validation result from {validator_name}: {type(outcome).__name__}."
                )
            if not ok:
                if isinstance(explicit_error, BaseException):
                    raise explicit_error
                raise _FinalValidationError(reason)

    @staticmethod
    def _ensure_value_is_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return bool(value) and all(
                LongOutputDelivery._ensure_value_is_present(item)
                for item in value
            )
        return True

    def _active_ensure_policies(
        self,
    ) -> dict[str, Literal["presence", "not_null"]]:
        auto_policies: dict[str, Literal["presence", "not_null"]] = {}
        schema = self.output_schema
        if (
            not isinstance(schema, (Mapping, Sequence))
            or isinstance(schema, (str, bytes))
        ):
            raise LongOutputError(
                "Structured long-output validation requires a mapping or sequence schema."
            )
        try:
            auto_policies = DataPathBuilder.extract_ensure_path_policies(
                schema,
                style=self.key_style,
            )
        except Exception:
            auto_policies = {}
        active: dict[str, Literal["presence", "not_null"]]
        if self.ensure_keys is None:
            active = dict(auto_policies)
        elif not self.ensure_keys:
            active = {}
        else:
            active = {
                **auto_policies,
                **{
                    path: auto_policies.get(path, "presence")
                    for path in self.ensure_keys
                },
            }
        strict_output = (
            bool(self.ensure_all_keys)
            if self.ensure_all_keys is not None
            else bool(
                getattr(self.prompt_object, "ensure_all_keys", False)
            )
        )
        if strict_output:
            for slot in self.slots:
                if slot.display_path != "$":
                    active.setdefault(slot.display_path, "presence")
        return active

    def _missing_ensure_paths(self, candidate: Any) -> list[str]:
        missing_paths: list[str] = []
        missing = object()
        for path, policy in self._active_ensure_policies().items():
            located = DataLocator.locate_path_in_dict(
                candidate,
                path,
                self.key_style,
                default=missing,
            )
            if located is missing:
                missing_paths.append(path)
            elif (
                policy == "not_null"
                and not self._ensure_value_is_present(located)
            ):
                missing_paths.append(path)
        return missing_paths

    def _validate_ensure_keys(self, candidate: Any) -> None:
        if self._field_continuation is not None:
            self._field_continuation.validate_ensure_keys(candidate)
            return
        missing_paths = self._missing_ensure_paths(candidate)
        if missing_paths:
            raise _FinalValidationError(
                "Final assembled output is missing or has an empty ensure key: "
                + ", ".join(missing_paths[:20])
                + "."
            )

    async def materialize_and_validate(self) -> Any:
        candidate = await self._replay_latest_manifest()
        self.value = candidate
        result_object: BaseModel | None = None
        if self.structured:
            strict_output = (
                bool(self.ensure_all_keys)
                if self.ensure_all_keys is not None
                else bool(getattr(self.prompt_object, "ensure_all_keys", False))
            )
            output_model = self.validation_prompt.to_output_model(
                strict_output=strict_output,
            )
            result_object = output_model.model_validate(candidate)
            self._validate_ensure_keys(candidate)
        await self._run_custom_validators(candidate, result_object)
        candidate_text = _canonical_json(candidate) if self.structured else str(candidate)
        final_ref = await self._write_verified(
            (
                f"long_output/{self.storage_id}/final/result.json"
                if self.structured
                else f"long_output/{self.storage_id}/final/result.txt"
            ),
            candidate_text,
        )
        self.final_result = candidate
        self.final_result_object = result_object
        self.final_ref = final_ref
        if not self.is_stage:
            self.execution._long_output_result_object = result_object
        validation_handlers = self.request.extension_handlers.get(
            "validate_handlers",
            [],
        )
        declared_coverage = bool(
            self.ensure_keys
            or self.validate_handler is not None
            or validation_handlers
        )
        self.meta = {
            "enabled": True,
            "status": "completed",
            "request_count": self.request_count,
            "segment_count": self.segment_index + 1,
            "manifest_revision": self.revision,
            "accepted_unit_count": len(self.units),
            "replayed_unit_count": self.replayed_unit_count,
            "validation_repair_count": self.validation_repair_count,
            "rejected_update_count": len(
                self.execution.diagnostics.get("long_output_rejected_updates", [])
            ),
            "no_progress_event_count": len(
                self.execution.diagnostics.get("long_output_no_progress", [])
            ),
            "manifest_ref": self.current_manifest_ref,
            "final_ref": final_ref,
            "final_ref_retention": "execution_private_staging",
            "final_digest": self.current_digest,
            "transport_complete": True,
            "schema_complete": True,
            "declared_coverage_complete": True if declared_coverage else None,
            "semantic_exhaustiveness": "not_claimed",
            "guarantee_level": (
                "transport_schema_and_declared_coverage"
                if declared_coverage
                else "transport_and_schema"
            ),
        }
        if not self.is_stage:
            self.execution._long_output_meta = dict(self.meta)
        self.execution.diagnostics["long_output"] = dict(self.meta)
        return candidate

    async def run_continuation_flow(self) -> Any:
        flow = TriggerFlow(name="agent-execution-long-output-delivery")

        async def request_segment(data: Any) -> None:
            outcome = await self.request_and_commit_next()
            await data.async_set_state(
                "manifest_revision",
                self.revision,
                emit=False,
            )
            if outcome["is_final"]:
                await data.async_emit_nowait("VALIDATE", None)
                return
            if self.no_progress_count >= _NO_PROGRESS_LIMIT:
                no_progress_events = self.execution.diagnostics.get(
                    "long_output_no_progress",
                    [],
                )
                last_no_progress = (
                    no_progress_events[-1]
                    if isinstance(no_progress_events, list)
                    and no_progress_events
                    and isinstance(no_progress_events[-1], Mapping)
                    else {}
                )
                raise LongOutputError(
                    "Long-output continuation made no durable progress in "
                    f"{_NO_PROGRESS_LIMIT} consecutive logical requests. "
                    "Last no-progress reason "
                    f"{last_no_progress.get('reason_code', 'unknown')}: "
                    f"{last_no_progress.get('reason', 'unavailable')}"
                )
            await data.async_emit_nowait("CONTINUE", None)

        async def validate_candidate(data: Any) -> None:
            try:
                result = await self.materialize_and_validate()
            except (_FinalValidationError, ValidationError) as error:
                if self.validation_repair_count >= self.max_retries:
                    raise
                self.validation_repair_count += 1
                if isinstance(error, ValidationError):
                    reason = json.dumps(
                        error.errors(include_input=False, include_url=False),
                        ensure_ascii=False,
                    )
                else:
                    reason = str(error)
                self.repair_feedback = {
                    "previous_segment_index": self.segment_index,
                    "accepted_prefix_count": 0,
                    "action": (
                        "Final validation failed. Preserve every accepted unit and "
                        "emit only the missing or additional updates needed to satisfy "
                        "the original output contract and declared validation rules."
                    ),
                    "reason": reason[:1000],
                }
                repairs = self.execution.diagnostics.setdefault(
                    "long_output_validation_repairs",
                    [],
                )
                if isinstance(repairs, list):
                    repairs.append(
                        {
                            "repair_attempt": self.validation_repair_count,
                            "manifest_revision": self.revision,
                            "reason": reason[:1000],
                        }
                    )
                await self.execution.emit_stream(
                    "long_output.validation_repair",
                    {
                        "repair_attempt": self.validation_repair_count,
                        "manifest_revision": self.revision,
                        "reason": reason[:500],
                    },
                    route="model_request",
                    source="long_output",
                    meta={"stream_kind": "status", "status": "repairing"},
                )
                await data.async_emit_nowait("CONTINUE", None)
                return
            await data.async_set_state("final_result", result, emit=False)
            await data.async_set_state(
                "final_digest",
                self.current_digest,
                emit=False,
            )

        flow.to(request_segment)
        flow.when("CONTINUE").to(request_segment)
        flow.when("VALIDATE").to(validate_candidate)
        flow_execution = flow.create_execution(
            auto_close=False,
            record_store=False,
            run_context=self.execution.agent_execution_run_context,
        )
        await flow_execution.async_start(None)
        await flow_execution.async_close(reason="long_output_complete")
        if self.final_result is None:
            raise LongOutputError("Long-output delivery flow closed without an accepted result.")
        return self.final_result
